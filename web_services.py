"""Read-mostly service layer for the local web dashboard (issue #6).

Rules that shape this module:

- Read paths never run CREATE/DROP. ``library.db`` is opened with SQLite
  ``mode=ro``; a missing or schema-incompatible index surfaces as
  ``index_unavailable`` / ``migration_required`` — never silently mutated
  (W0). ``pipeline.search_index`` is reused *after* that check passes, so
  its idempotent ``CREATE IF NOT EXISTS`` stays a no-op.
- ``operations.db`` (costs, scoring, digests, tasks) is likewise only
  read when present; missing data renders as ``available: false`` —
  unknown is never displayed as zero (W1/W5).
- All SQL is parameterized and sort fields come from an allowlist (W8).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 200
MAX_TOP_K = 50
VIDEO_SORT_COLUMNS = {
    "indexed_at": "videos.indexed_at",
    "title": "videos.title",
    "duration": "videos.duration",
    "status": "videos.status",
    "job": "videos.job",
}
REQUIRED_INDEX_SCHEMA = {
    "videos": {"job", "title", "source", "status", "duration", "indexed_at", "categories"},
    "events": {"job", "timestamp", "kind", "text"},
    "index_state": {"job", "signature", "row_count"},
    "vectors": {"text_hash", "model", "dim", "vector"},
}
OPERATIONS_READ_TABLES = (
    "usage_costs",
    "video_scores",
    "digest_runs",
    "notification_deliveries",
    "notification_events",
)
PLATFORM_HOSTS = (
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("facebook.com", "Facebook"), ("fb.watch", "Facebook"),
    ("instagram.com", "Instagram"), ("threads.net", "Threads"),
    ("vimeo.com", "Vimeo"), ("tiktok.com", "TikTok"),
)


class ServiceError(Exception):
    """Safe-to-display failure with an API error code."""

    def __init__(self, code: str, safe_message: str, *, retryable: bool = False, http_status: int = 503):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.http_status = http_status


class MigrationRequired(ServiceError):
    def __init__(self, detail: str = "library.db schema incompatible"):
        super().__init__("migration_required", detail, retryable=False, http_status=503)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime.datetime | None:
    import digests

    return digests.parse_iso_datetime(str(value) if value else None)


# ---------------------------------------------------------------------------
# Connections (read-only; never CREATE/DROP on read paths)
# ---------------------------------------------------------------------------


def connect_library_ro(root: Path) -> sqlite3.Connection:
    db_path = root / "library.db"
    if not db_path.is_file():
        raise ServiceError(
            "index_unavailable",
            "library.db not present; run `pipeline.py --index` first",
            http_status=503,
        )
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ServiceError("index_unavailable", "library.db unreadable", http_status=503) from exc
    conn.row_factory = sqlite3.Row
    try:
        _assert_index_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _assert_index_schema(conn: sqlite3.Connection) -> None:
    """Reject schemas index_connection() would migrate — reads stay read-only."""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    missing = [name for name in REQUIRED_INDEX_SCHEMA if name not in tables]
    if missing:
        raise MigrationRequired(f"missing tables: {', '.join(sorted(missing))}")
    for table, required_cols in REQUIRED_INDEX_SCHEMA.items():
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not required_cols.issubset(cols):
            raise MigrationRequired(f"table {table} missing columns")


def operations_db_available(root: Path) -> bool:
    return (root / "operations.db").is_file()


def connect_operations_ro(root: Path) -> sqlite3.Connection:
    db_path = root / "operations.db"
    if not db_path.is_file():
        raise ServiceError("operations_unavailable", "operations.db not present", http_status=503)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _operations_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


# ---------------------------------------------------------------------------
# Job manifest helpers (W1/W2)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _job_dir(root: Path, job: str) -> Path | None:
    if not JOB_ID_RE.match(job or ""):
        return None
    jobs_root = (root / "jobs").resolve()
    candidate = (jobs_root / job).resolve()
    try:
        candidate.relative_to(jobs_root)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _manifest_for(root: Path, job: str) -> dict[str, Any]:
    job_dir = _job_dir(root, job)
    if job_dir is None:
        return {}
    return _read_json(job_dir / "manifest.json")


def _first_completed_at(manifest: dict[str, Any], job_dir: Path | None) -> tuple[str | None, str]:
    """Stable completion timestamp — never indexed_at (reindexing is not 'new')."""
    for field in ("first_completed_at", "completed_at", "eligible_at", "created_at"):
        dt = _parse_iso(manifest.get(field))
        if dt:
            return _iso(dt), field
    if job_dir is not None:
        manifest_path = job_dir / "manifest.json"
        try:
            return _iso(datetime.datetime.fromtimestamp(manifest_path.stat().st_mtime, datetime.timezone.utc)), "mtime"
        except OSError:
            pass
    return None, "unknown"


def _platform_of(url: str) -> str:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    for needle, name in PLATFORM_HOSTS:
        if needle in host:
            return name
    return "local" if not host else "web"


def _author_of(manifest: dict[str, Any]) -> str | None:
    source = manifest.get("source") or {}
    for key in ("author", "uploader", "channel", "channel_name", "page_name"):
        value = source.get(key) or manifest.get(key)
        if value:
            return str(value)
    return None


def _published_at(manifest: dict[str, Any]) -> str | None:
    source = manifest.get("source") or {}
    for key in ("published_at", "upload_date", "release_date", "published"):
        dt = _parse_iso(source.get(key) or manifest.get(key))
        if dt:
            return _iso(dt)
    return None


def _latest_scores(root: Path) -> dict[str, dict[str, Any]]:
    """job -> latest video_scores row (importance/priority), if available."""
    if not operations_db_available(root):
        return {}
    try:
        with closing(connect_operations_ro(root)) as conn:
            if "video_scores" not in _operations_table_names(conn):
                return {}
            rows = conn.execute(
                """
                SELECT job, importance, relevance, novelty, priority_score, status, scored_at
                FROM video_scores
                WHERE (job, scored_at) IN (
                    SELECT job, MAX(scored_at) FROM video_scores GROUP BY job
                )
                """
            ).fetchall()
        return {row["job"]: dict(row) for row in rows}
    except (sqlite3.Error, ServiceError):
        return {}


# ---------------------------------------------------------------------------
# W1 — dashboard stats
# ---------------------------------------------------------------------------


def dashboard_stats(root: Path) -> dict[str, Any]:
    now = _utc_now()
    local_tz = datetime.datetime.now().astimezone().tzinfo or datetime.timezone.utc
    today_start_local = datetime.datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_local = today_start_local - datetime.timedelta(days=today_start_local.weekday())

    new_today = 0
    new_week = 0
    jobs_dir = root / "jobs"
    videos_by_status: dict[str, int] = {}
    index_ok = False
    if jobs_dir.is_dir():
        for manifest_path in jobs_dir.glob("*/manifest.json"):
            manifest = _read_json(manifest_path)
            if not manifest:
                continue
            completed_at, _field = _first_completed_at(manifest, manifest_path.parent)
            completed_dt = _parse_iso(completed_at)
            if completed_dt is None:
                continue
            if completed_dt >= today_start_local.astimezone(datetime.timezone.utc):
                new_today += 1
            if completed_dt >= week_start_local.astimezone(datetime.timezone.utc):
                new_week += 1
    try:
        with closing(connect_library_ro(root)) as conn:
            index_ok = True
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status"):
                videos_by_status[str(row["status"] or "unknown")] = int(row["n"])
    except ServiceError:
        index_ok = False

    rss_counts: dict[str, int] = {}
    rss_updated_at: str | None = None
    state_path = root / "rss" / "state.json"
    if state_path.is_file():
        try:
            import pipeline

            state = pipeline.load_rss_state(root)
            rss_updated_at = _iso(
                datetime.datetime.fromtimestamp(state_path.stat().st_mtime, datetime.timezone.utc)
            )
            for item in state["items"]:
                status = str(item.get("status") or "unknown")
                if status == "processing":
                    started = _parse_iso(item.get("processing_started_at"))
                    lease = float(getattr(pipeline, "RSS_LEASE_SECONDS", 7200))
                    if started and (now - started).total_seconds() > lease:
                        status = "unknown"
                rss_counts[status] = rss_counts.get(status, 0) + 1
        except Exception:
            rss_counts = {"unreadable": 1}

    scoring_available = False
    high_importance = 0
    scores = _latest_scores(root)
    if scores:
        scoring_available = True
        high_importance = sum(1 for s in scores.values() if (s.get("importance") or 0) >= 8)

    return {
        "schema_version": "1",
        "as_of": _iso(now),
        "timezone": str(local_tz),
        "scope": "local",
        "window": {
            "today_start": _iso(today_start_local),
            "week_start": _iso(week_start_local),
        },
        "new_videos": {"today": new_today, "this_week": new_week, "field": "first_completed_at/eligible_at"},
        "videos": {"indexed": index_ok, "by_status": videos_by_status},
        "rss": {
            "queued": rss_counts.get("queued", 0),
            "processing": rss_counts.get("processing", 0),
            "failed": rss_counts.get("failed", 0),
            "dead_letter": rss_counts.get("dead-letter", 0),
            "unknown": rss_counts.get("unknown", 0),
            "updated_at": rss_updated_at,
        },
        "scoring": {
            "available": scoring_available,
            "high_importance": high_importance if scoring_available else None,
            "label": "importance >= 8" if scoring_available else "尚未評分",
        },
        "cost": cost_summary(root),
        "blocked_operations": budget_blocked_count(root),
    }


def budget_blocked_count(root: Path) -> int | None:
    if not operations_db_available(root):
        return None
    try:
        with closing(connect_operations_ro(root)) as conn:
            if "usage_costs" not in _operations_table_names(conn):
                return None
            row = conn.execute(
                "SELECT COUNT(*) FROM usage_costs WHERE status = 'blocked_budget'"
            ).fetchone()
            return int(row[0])
    except (sqlite3.Error, ServiceError):
        return None


# ---------------------------------------------------------------------------
# W2 — library list & detail
# ---------------------------------------------------------------------------


def list_videos_page(
    root: Path,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: str = "indexed_at",
    order: str = "desc",
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    filters = filters or {}
    if page < 1:
        raise ServiceError("invalid_param", "page must be >= 1", http_status=400)
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ServiceError("invalid_param", f"page_size must be 1..{MAX_PAGE_SIZE}", http_status=400)
    if sort not in VIDEO_SORT_COLUMNS:
        raise ServiceError("invalid_param", f"sort must be one of {sorted(VIDEO_SORT_COLUMNS)}", http_status=400)
    if order not in ("asc", "desc"):
        raise ServiceError("invalid_param", "order must be asc or desc", http_status=400)

    where: list[str] = []
    params: list[Any] = []
    job = str(filters.get("job") or "").strip()
    if job:
        if not JOB_ID_RE.match(job):
            raise ServiceError("invalid_param", "invalid job id", http_status=400)
        where.append("videos.job = ?")
        params.append(job)
    status = str(filters.get("status") or "").strip()
    if status:
        if not re.fullmatch(r"[A-Za-z_-]{1,32}", status):
            raise ServiceError("invalid_param", "invalid status", http_status=400)
        where.append("videos.status = ?")
        params.append(status)
    category = str(filters.get("category") or "").strip()
    if category:
        if len(category) > 64:
            raise ServiceError("invalid_param", "category too long", http_status=400)
        # exact array membership — no substring false-positives (W2)
        where.append("EXISTS (SELECT 1 FROM json_each(videos.categories) je WHERE je.value = ?)")
        params.append(category)
    date_from = _parse_iso(filters.get("date_from"))
    date_to = _parse_iso(filters.get("date_to"))
    if filters.get("date_from") and date_from is None:
        raise ServiceError("invalid_param", "date_from must be ISO-8601", http_status=400)
    if filters.get("date_to") and date_to is None:
        raise ServiceError("invalid_param", "date_to must be ISO-8601", http_status=400)
    date_field = str(filters.get("date_field") or "indexed_at")
    if date_field not in ("indexed_at", "first_completed_at", "published_at"):
        raise ServiceError(
            "invalid_param", "date_field must be indexed_at/first_completed_at/published_at", http_status=400
        )
    manifest_date_range = None
    if date_from or date_to:
        if date_field == "indexed_at":
            if date_from:
                where.append("videos.indexed_at >= ?")
                params.append(_iso(date_from))
            if date_to:
                where.append("videos.indexed_at <= ?")
                params.append(_iso(date_to))
        else:
            manifest_date_range = (date_field, date_from, date_to)

    # filters that live in manifests (author / platform / manifest date
    # fields) resolve to a job set first — pagination stays exact.
    author = str(filters.get("author") or "").strip().lower()
    platform = str(filters.get("platform") or "").strip().lower()
    if author or platform or manifest_date_range:
        matched = _manifest_job_filter(
            root, author=author, platform=platform, date_range=manifest_date_range
        )
        if not matched:
            return {"items": [], "page": page, "page_size": page_size, "total": 0,
                    "sort": sort, "order": order, "filters": filters}
        where.append(f"videos.job IN ({','.join('?' for _ in matched)})")
        params.extend(sorted(matched))

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with closing(connect_library_ro(root)) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM videos {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT videos.job, videos.title, videos.source, videos.status,
                   videos.duration, videos.indexed_at, videos.categories
            FROM videos {clause}
            ORDER BY {VIDEO_SORT_COLUMNS[sort]} {order.upper()}, videos.job
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()

    scores = _latest_scores(root)
    items = []
    for row in rows:
        manifest = _manifest_for(root, row["job"])
        completed_at, completed_field = _first_completed_at(manifest, _job_dir(root, row["job"]))
        try:
            categories = json.loads(row["categories"] or "[]")
        except (json.JSONDecodeError, TypeError):
            categories = []
        score = scores.get(row["job"])
        items.append(
            {
                "job": row["job"],
                "title": row["title"],
                "source": row["source"],
                "platform": _platform_of(row["source"]),
                "author": _author_of(manifest),
                "published_at": _published_at(manifest),
                "first_completed_at": completed_at,
                "first_completed_field": completed_field,
                "duration_seconds": row["duration"],
                "categories": categories,
                "status": row["status"],
                "score": (
                    {
                        "importance": score.get("importance"),
                        "priority_score": score.get("priority_score"),
                        "scored_at": score.get("scored_at"),
                    }
                    if score
                    else None
                ),
            }
        )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": int(total),
        "sort": sort,
        "order": order,
        "filters": {k: v for k, v in filters.items() if v},
    }


def _manifest_job_filter(
    root: Path,
    *,
    author: str,
    platform: str,
    date_range: tuple[str, datetime.datetime | None, datetime.datetime | None] | None = None,
) -> set[str]:
    jobs_dir = root / "jobs"
    matched: set[str] = set()
    if not jobs_dir.is_dir():
        return matched
    for manifest_path in jobs_dir.glob("*/manifest.json"):
        manifest = _read_json(manifest_path)
        if not manifest:
            continue
        if author:
            value = (_author_of(manifest) or "").lower()
            if author not in value:
                continue
        if platform:
            url = str((manifest.get("source") or {}).get("url") or "")
            if _platform_of(url).lower() != platform:
                continue
        if date_range:
            field, date_from, date_to = date_range
            if field == "first_completed_at":
                value_s, _f = _first_completed_at(manifest, manifest_path.parent)
                value_dt = _parse_iso(value_s)
            else:
                value_dt = _parse_iso(_published_at(manifest))
            if value_dt is None:
                continue
            if date_from and value_dt < date_from:
                continue
            if date_to and value_dt > date_to:
                continue
        matched.add(manifest_path.parent.name)
    return matched


def get_video_detail(
    root: Path,
    job: str,
    *,
    timeline_offset: int = 0,
    timeline_limit: int = 200,
) -> dict[str, Any]:
    if not JOB_ID_RE.match(job or ""):
        raise ServiceError("invalid_param", "invalid job id", http_status=400)
    job_dir = _job_dir(root, job)
    if job_dir is None:
        raise ServiceError("not_found", "unknown job", http_status=404)

    manifest = _read_json(job_dir / "manifest.json")
    summary = _read_json(job_dir / "summary.json")
    timeline = _read_json(job_dir / "timeline.json")
    entries = timeline.get("entries") or timeline.get("chapters") or []
    total_entries = len(entries)
    timeline_limit = max(1, min(int(timeline_limit), 500))
    timeline_offset = max(0, int(timeline_offset))
    window = entries[timeline_offset : timeline_offset + timeline_limit]

    transcript_path = job_dir / "transcript.txt"
    transcript: dict[str, Any] = {"available": False}
    for name in ("transcript.txt", "transcript_verified.txt", "whisper.txt"):
        candidate = job_dir / name
        if candidate.is_file():
            transcript = {"available": True, "artifact": name, "size_bytes": candidate.stat().st_size}
            break

    completed_at, completed_field = _first_completed_at(manifest, job_dir)
    score = _latest_scores(root).get(job)
    related: list[dict[str, Any]] = []
    try:
        categories = {str(c) for c in (manifest.get("categories") or [])}
        if categories:
            with closing(connect_library_ro(root)) as conn:
                placeholders = ",".join("?" for _ in categories)
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT videos.job, videos.title FROM videos
                    WHERE videos.job != ? AND EXISTS (
                        SELECT 1 FROM json_each(videos.categories) je WHERE je.value IN ({placeholders})
                    ) LIMIT 10
                    """,
                    (job, *sorted(categories)),
                ).fetchall()
                related = [{"job": r["job"], "title": r["title"]} for r in rows]
    except ServiceError:
        related = []

    operations: dict[str, Any] = {"costs": [], "tasks": []}
    if operations_db_available(root):
        try:
            with closing(connect_operations_ro(root)) as conn:
                tables = _operations_table_names(conn)
                if "usage_costs" in tables:
                    operations["costs"] = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT operation, provider, status, cost_status, estimated_micro_usd, created_at"
                            " FROM usage_costs WHERE job = ? ORDER BY created_at DESC LIMIT 20",
                            (job,),
                        ).fetchall()
                    ]
                if "operation_tasks" in tables:
                    operations["tasks"] = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT task_id, type, status, safe_error, created_at FROM operation_tasks"
                            " WHERE job = ? ORDER BY created_at DESC LIMIT 20",
                            (job,),
                        ).fetchall()
                    ]
        except (sqlite3.Error, ServiceError):
            pass

    return {
        "schema_version": "1",
        "job": job,
        "title": (manifest.get("source") or {}).get("title") or job,
        "status": manifest.get("status", "unknown"),
        "source": manifest.get("source") or {},
        "platform": _platform_of(str((manifest.get("source") or {}).get("url") or "")),
        "author": _author_of(manifest),
        "published_at": _published_at(manifest),
        "first_completed_at": completed_at,
        "first_completed_field": completed_field,
        "duration_seconds": (manifest.get("video") or {}).get("duration_seconds"),
        "categories": manifest.get("categories") or [],
        "summary": {
            "brief": summary.get("brief") or summary.get("summary"),
            "terms": summary.get("terms") or [],
            "status": summary.get("status", "unknown"),
            # video-level summaries carry no precise timestamp — label the
            # level instead of pretending the text happened at 00:00 (W2).
            "time_scope": "video_level",
        },
        "timeline": {
            "entries": window,
            "offset": timeline_offset,
            "limit": timeline_limit,
            "total": total_entries,
        },
        "transcript": transcript,
        "score": score,
        "related": related,
        "operations": operations,
    }


def get_transcript_window(root: Path, job: str, *, offset: int = 0, limit: int = 50_000) -> dict[str, Any]:
    """Windowed transcript read — large transcripts never load fully into RAM (W2/W10)."""
    if not JOB_ID_RE.match(job or ""):
        raise ServiceError("invalid_param", "invalid job id", http_status=400)
    job_dir = _job_dir(root, job)
    if job_dir is None:
        raise ServiceError("not_found", "unknown job", http_status=404)
    for name in ("transcript.txt", "transcript_verified.txt", "whisper.txt"):
        candidate = job_dir / name
        if candidate.is_file():
            limit = max(1, min(int(limit), 200_000))
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(max(0, int(offset)))
                chunk = handle.read(limit)
            return {
                "artifact": name,
                "offset": max(0, int(offset)),
                "limit": limit,
                "text": chunk,
                "size_bytes": candidate.stat().st_size,
            }
    raise ServiceError("not_found", "no transcript artifact", http_status=404)


# ---------------------------------------------------------------------------
# W3 — local search boundary + guarded paid enqueues
# ---------------------------------------------------------------------------


def search_local(root: Path, query: str, limit: int = 20, category: str = "") -> dict[str, Any]:
    """GET search: local FTS / stored results only. No embeddings, no LLM,
    no rerank, no model downloads — zero external calls (W3)."""
    query = (query or "").strip()
    if not query:
        raise ServiceError("invalid_param", "q must be non-empty", http_status=400)
    if len(query) > MAX_QUERY_LENGTH:
        raise ServiceError("invalid_param", f"q must be <= {MAX_QUERY_LENGTH} chars", http_status=400)
    limit = max(1, min(int(limit), MAX_TOP_K))
    if category and len(category) > 64:
        raise ServiceError("invalid_param", "category too long", http_status=400)

    # Schema check first: search_index reuses index_connection which would
    # CREATE/migrate; we only delegate when the existing schema is intact.
    with closing(connect_library_ro(root)):
        pass
    import pipeline

    try:
        hits = pipeline.search_index(root, query, limit, category)
    except RuntimeError as exc:
        raise ServiceError("search_failed", str(exc)[:200], http_status=400) from exc
    results = [
        {
            "job": hit.get("job"),
            "title": hit.get("title"),
            "source": hit.get("source"),
            "timestamp": hit.get("timestamp"),
            "kind": hit.get("kind"),
            "snippet": hit.get("snippet"),
            "mode": hit.get("mode"),
        }
        for hit in hits
    ]
    return {
        "schema_version": "1",
        "mode": "local",
        "external_calls": 0,
        "query": query,
        "limit": limit,
        "results": results,
    }


def _budget_guard(root: Path, operation: str, estimated_micro_usd: int) -> str | None:
    """Reserve budget before enqueueing a paid task (backend-side, W3)."""
    import cost_tracker

    allowed, reservation_id, reason = cost_tracker.check_and_reserve_budget(
        root, operation, estimated_micro_usd
    )
    if not allowed:
        raise ServiceError("budget_blocked", reason or "budget exceeded", http_status=403)
    return reservation_id


def enqueue_semantic_search(
    root: Path, query: str, *, limit: int = 20, idempotency_key: str | None = None
) -> dict[str, Any]:
    import web_tasks

    query = (query or "").strip()
    if not query:
        raise ServiceError("invalid_param", "query required", http_status=400)
    if len(query) > MAX_QUERY_LENGTH:
        raise ServiceError("invalid_param", f"query must be <= {MAX_QUERY_LENGTH} chars", http_status=400)
    limit = max(1, min(int(limit), MAX_TOP_K))
    reservation_id = _budget_guard(root, "semantic_search", 50_000)  # conservative $0.05 cap estimate
    task = web_tasks.enqueue_task(
        root,
        "semantic_search",
        payload={
            "query": query,
            "limit": limit,
            "backend": "openrouter",
            "model": os.getenv("EMBED_MODEL") or "",
            "reservation_id": reservation_id,
        },
        dedup_key=idempotency_key or None,
    )
    return _task_accepted(task, mode="semantic")


def enqueue_ask(
    root: Path, question: str, *, limit: int = 12, idempotency_key: str | None = None
) -> dict[str, Any]:
    import web_tasks

    question = (question or "").strip()
    if not question:
        raise ServiceError("invalid_param", "question required", http_status=400)
    if len(question) > MAX_QUERY_LENGTH:
        raise ServiceError("invalid_param", f"question must be <= {MAX_QUERY_LENGTH} chars", http_status=400)
    limit = max(1, min(int(limit), 30))
    reservation_id = _budget_guard(root, "ask", 200_000)  # conservative $0.20 cap estimate
    task = web_tasks.enqueue_task(
        root,
        "ask",
        payload={"question": question, "limit": limit, "reservation_id": reservation_id},
        dedup_key=idempotency_key or None,
    )
    return _task_accepted(task, mode="ask")


def enqueue_rss_retry(root: Path, item_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
    """Validate the RSS item state before queueing a retry (W4).

    ``processing`` items are 'unknown', not 'failed': a fresh processing
    lease means another worker may still own it — the caller must wait or
    reconcile, not rebuild a paid job with one click.
    """
    import pipeline
    import web_tasks

    state = pipeline.load_rss_state(root)
    item = next((entry for entry in state["items"] if entry.get("id") == item_id), None)
    if item is None:
        raise ServiceError("not_found", "unknown job/item", http_status=404)
    status = str(item.get("status") or "")
    if status == "processing":
        started = _parse_iso(item.get("processing_started_at"))
        lease = float(getattr(pipeline, "RSS_LEASE_SECONDS", 7200))
        if started and (_utc_now() - started).total_seconds() <= lease:
            raise ServiceError(
                "unknown_state",
                "item still within processing lease; reconcile remote state before retry",
                http_status=409,
            )
    if status not in {"failed", "dead-letter", "processing"}:
        raise ServiceError("not_retryable", f"status {status!r} is not retryable", http_status=409)
    task = web_tasks.enqueue_task(
        root,
        "rss_item_retry",
        payload={"item_id": item_id},
        # attempts is part of the key: a double-click on the same failure
        # dedups, while a later *new* failure round can enqueue again.
        dedup_key=idempotency_key or f"rss_retry:{item_id}:{status}:{item.get('attempts', 0)}",
    )
    return _task_accepted(task, mode="rss_item_retry")


def _task_accepted(task: dict[str, Any], *, mode: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "task_id": task["task_id"],
        "type": task["type"],
        "status": task["status"],
        "mode": mode,
        "deduplicated": not task.get("created", True),
        "accepted": True,
        "note": "task queued; poll GET /api/tasks/{task_id} — acceptance is not completion",
    }


# ---------------------------------------------------------------------------
# W4 — operations views
# ---------------------------------------------------------------------------


_SECRETISH = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|authorization|cookie|signature|sig|credential)"
    r"\s*[:=]\s*[^\s&]+"
)


def sanitize_error(text: Any) -> str | None:
    """Errors shown in the UI are safe summaries — tokens, cookies, signed
    URL params and raw provider responses never reach the frontend (W4)."""
    if text is None:
        return None
    cleaned = _SECRETISH.sub("[redacted]", str(text))
    return " ".join(cleaned.split())[:300]


def rss_overview(root: Path) -> dict[str, Any]:
    import pipeline

    state = pipeline.load_rss_state(root)
    state_path = pipeline.rss_state_path(root)
    updated_at = None
    try:
        updated_at = _iso(datetime.datetime.fromtimestamp(state_path.stat().st_mtime, datetime.timezone.utc))
    except OSError:
        pass
    items = []
    for item in state["items"][-200:]:
        status = str(item.get("status") or "unknown")
        if status == "processing":
            started = _parse_iso(item.get("processing_started_at"))
            lease = float(getattr(pipeline, "RSS_LEASE_SECONDS", 7200))
            if started and (_utc_now() - started).total_seconds() > lease:
                status = "unknown"
        items.append(
            {
                "id": item.get("id"),
                "subscription_id": item.get("subscription_id"),
                "title": item.get("title"),
                "status": status,
                "attempts": item.get("attempts", 0),
                "safe_error": sanitize_error(item.get("last_error")),
                "discovered_at": item.get("discovered_at"),
                "next_retry_at": item.get("next_retry_at") or None,
            }
        )
    subscriptions = [
        {
            "id": sub.get("id"),
            "kind": sub.get("kind", "rss"),
            "name": sub.get("name"),
            "enabled": bool(sub.get("enabled", True)),
            "last_checked": sub.get("last_checked"),
            "safe_error": sanitize_error(sub.get("last_error")),
        }
        for sub in state["subscriptions"]
    ]
    return {
        "schema_version": "1",
        "source": "rss/state.json",
        "updated_at": updated_at,
        "subscriptions": subscriptions,
        "items": items,
    }


def failed_operations(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"schema_version": "1", "rss_failed": [], "cost_failed": [], "tasks_failed": []}
    try:
        overview = rss_overview(root)
        out["rss_failed"] = [i for i in overview["items"] if i["status"] in {"failed", "dead-letter", "unknown"}]
    except Exception:
        out["rss_failed"] = []
    if operations_db_available(root):
        try:
            import cost_tracker

            out["cost_failed"] = [
                {**row, "safe_error": sanitize_error(row.get("safe_error"))}
                for row in cost_tracker.get_failed_list(root)
            ]
        except Exception:
            out["cost_failed"] = []
        try:
            import web_tasks

            out["tasks_failed"] = [
                {k: t.get(k) for k in ("task_id", "type", "status", "safe_error", "attempts", "created_at")}
                for t in web_tasks.list_tasks(root, status="failed", limit=50)
                + web_tasks.list_tasks(root, status="unknown", limit=50)
            ]
        except Exception:
            out["tasks_failed"] = []
    out["items"] = (
        [{"kind": "rss_item", **i} for i in out["rss_failed"]]
        + [{"kind": "usage_cost", **i} for i in out["cost_failed"]]
        + [{"kind": "operation_task", **i} for i in out["tasks_failed"]]
    )
    return out


# ---------------------------------------------------------------------------
# W5 — cost & budget reads (local only, never unknown-as-zero)
# ---------------------------------------------------------------------------


def cost_summary(root: Path) -> dict[str, Any]:
    if not operations_db_available(root):
        return {"available": False, "reason": "operations.db not present"}
    try:
        import cost_tracker

        report = cost_tracker.get_cost_report(root)
        return {
            "available": True,
            "period": report.get("period"),
            "period_type": report.get("period_type"),
            "grand_total_estimated_usd": report.get("grand_total_estimated_usd"),
            "providers": report.get("providers", []),
            "note": "estimated usage recorded by this program; not a billing invoice",
        }
    except Exception as exc:
        return {"available": False, "reason": _safe_reason(exc)}


def _safe_reason(exc: BaseException) -> str:
    text = sanitize_error(str(exc)) or "unavailable"
    return text[:200]


def cost_report(root: Path, period_type: str = "daily") -> dict[str, Any]:
    if period_type not in ("daily", "monthly"):
        raise ServiceError("invalid_param", "period must be daily or monthly", http_status=400)
    if not operations_db_available(root):
        return {"available": False, "reason": "operations.db not present"}
    import cost_tracker

    return {
        "schema_version": "1",
        "available": True,
        "report": cost_tracker.get_cost_report(root, period_type=period_type),
        "note": "covers only calls made through this program",
    }


def budget_status(root: Path) -> dict[str, Any]:
    if not operations_db_available(root):
        return {"available": False, "reason": "operations.db not present"}
    import cost_tracker

    return {"schema_version": "1", "available": True, "status": cost_tracker.get_budget_status(root)}


def provider_health(root: Path) -> dict[str, Any]:
    if not operations_db_available(root):
        return {"available": False, "providers": [], "reason": "operations.db not present"}
    import cost_tracker

    data = cost_tracker.get_provider_health(root)
    # small samples are observations, not an SLA claim (W1)
    data["schema_version"] = "1"
    data["available"] = True
    data["note"] = "sample-based local telemetry, not provider SLA"
    return data


# ---------------------------------------------------------------------------
# W6 — digest preview / send handshake + history reads
# ---------------------------------------------------------------------------


def digest_content_signature(payload: dict[str, Any]) -> str:
    """Stable fingerprint of digest *content* — excludes volatile built_at so
    identical previews stay idempotent (W6)."""
    stable = {
        "digest_type": payload.get("digest_type"),
        "window_start": payload.get("window_start"),
        "window_end": payload.get("window_end"),
        "selected": [item.get("job") for item in payload.get("selected_items") or []],
        "deferred": payload.get("deferred_jobs") or [],
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def preview_digest(root: Path, digest_type: str = "daily", date_str: str | None = None) -> dict[str, Any]:
    """Local-only draft: computes the frozen payload and records a previewed
    digest_runs row. Zero remote calls (W6)."""
    import digests
    import notifications

    if digest_type not in ("daily", "weekly"):
        raise ServiceError("invalid_param", "digest_type must be daily or weekly", http_status=400)
    if date_str:
        try:
            datetime.date.fromisoformat(date_str)
        except ValueError as exc:
            raise ServiceError("invalid_param", "date must be YYYY-MM-DD", http_status=400) from exc

    tz_name = os.getenv("DIGEST_TIMEZONE", "Asia/Taipei")
    win_start, win_end, win_label = digests.compute_digest_window(digest_type, date_str, tz_name=tz_name)
    candidates = digests.collect_digest_candidates(root, win_start, win_end)
    payload = digests.build_digest_payload(candidates, win_start, win_end, win_label, digest_type=digest_type)
    signature = digest_content_signature(payload)
    recipient = os.getenv("EMAIL_TO", "").strip() or "default"
    schedule_id = f"web_{digest_type}"

    with closing(notifications.get_operations_db(root)) as conn:
        # already notified recipients are visible in the preview (W6)
        already = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT job FROM digest_items WHERE disposition IN ('delivered','pending')"
            ).fetchall()
        }
        cur = conn.execute(
            """
            SELECT digest_id, revision, rendered_payload_hash, selection_snapshot
            FROM digest_runs
            WHERE schedule_id = ? AND type = ? AND window_start = ? AND window_end = ? AND recipient_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (schedule_id, digest_type, win_start.isoformat(), win_end.isoformat(), recipient),
        )
        existing = cur.fetchone()
        if existing:
            stored_sig = (json.loads(existing["selection_snapshot"] or "{}")).get("content_signature")
            if stored_sig == signature:
                return _digest_preview_response(dict(existing), payload, tz_name, already)
            revision = int(existing["revision"]) + 1
        else:
            revision = 1
        digest_id = str(uuid.uuid4())
        snapshot = {"content_signature": signature, "items": payload["selected_items"]}
        with conn:
            conn.execute(
                """
                INSERT INTO digest_runs (
                    digest_id, schedule_id, type, window_start, window_end, timezone, recipient_id,
                    revision, selection_snapshot, rendered_payload_hash, status,
                    candidate_count, selected_count, deferred_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'previewed', ?, ?, ?, ?)
                """,
                (
                    digest_id,
                    schedule_id,
                    digest_type,
                    win_start.isoformat(),
                    win_end.isoformat(),
                    tz_name,
                    recipient,
                    revision,
                    json.dumps(snapshot, ensure_ascii=False),
                    payload["rendered_payload_hash"],
                    payload["candidate_count"],
                    payload["selected_count"],
                    payload["deferred_count"],
                    _iso(_utc_now()),
                ),
            )
            for rank, item in enumerate(payload["selected_items"], 1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO digest_items (digest_id, job, analysis_revision, rank, reason, disposition)
                    VALUES (?, ?, ?, ?, 'selected_by_time', 'previewed')
                    """,
                    (digest_id, item["job"], item.get("analysis_revision", "1"), rank),
                )
        row = {
            "digest_id": digest_id,
            "revision": revision,
            "rendered_payload_hash": payload["rendered_payload_hash"],
        }
    return _digest_preview_response(row, payload, tz_name, already)


def _digest_preview_response(
    row: dict[str, Any], payload: dict[str, Any], tz_name: str, already_notified: set[str]
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "digest_id": row["digest_id"],
        "revision": row["revision"],
        "payload_hash": row["rendered_payload_hash"],
        "digest_type": payload["digest_type"],
        "window": {
            "start": payload["window_start"],
            "end": payload["window_end"],
            "label": payload["window_label"],
            "timezone": tz_name,
        },
        "counts": {
            "candidates": payload["candidate_count"],
            "selected": payload["selected_count"],
            "deferred": payload["deferred_count"],
        },
        "already_notified_jobs": sorted(j for j in already_notified if j),
        "selected_items": [
            {"job": i.get("job"), "title": i.get("title"), "eligible_at": i.get("eligible_at")}
            for i in payload.get("selected_items") or []
        ],
        "remote_calls": 0,
    }


def confirm_digest_send(
    root: Path,
    digest_id: str,
    revision: int,
    payload_hash: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Verify digest_id + revision + payload_hash before queueing send (W6).

    If content or recipients changed since preview, the hash check rejects
    the stale confirmation — the caller must re-preview.
    """
    import notifications
    import web_tasks

    if not digest_id or not payload_hash:
        raise ServiceError("invalid_param", "digest_id and payload_hash required", http_status=400)
    with closing(notifications.get_operations_db(root)) as conn:
        row = conn.execute("SELECT * FROM digest_runs WHERE digest_id = ?", (digest_id,)).fetchone()
    if row is None:
        raise ServiceError("not_found", "unknown digest_id", http_status=404)
    if int(row["revision"]) != int(revision) or row["rendered_payload_hash"] != payload_hash:
        raise ServiceError(
            "stale_preview",
            "digest content changed since preview; re-preview required",
            http_status=409,
        )
    if str(row["status"]) in ("accepted", "delivered"):
        raise ServiceError("already_sent", "digest already dispatched", http_status=409)
    task = web_tasks.enqueue_task(
        root,
        "digest_send",
        job=None,
        revision=str(revision),
        payload={
            "digest_id": digest_id,
            "revision": int(revision),
            "payload_hash": payload_hash,
            "digest_type": row["type"],
            "date_str": None,
        },
        dedup_key=idempotency_key or f"digest_send:{digest_id}:r{revision}:{payload_hash[:16]}",
    )
    return _task_accepted(task, mode="digest_send")


def list_digests(root: Path, limit: int = 50) -> dict[str, Any]:
    if not operations_db_available(root):
        return {"items": [], "available": False}
    limit = max(1, min(int(limit), 200))
    try:
        with closing(connect_operations_ro(root)) as conn:
            if "digest_runs" not in _operations_table_names(conn):
                return {"items": [], "available": False}
            rows = conn.execute(
                """
                SELECT digest_id, type, window_start, window_end, timezone, recipient_id,
                       revision, rendered_payload_hash, status, candidate_count,
                       selected_count, deferred_count, created_at
                FROM digest_runs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"schema_version": "1", "available": True, "items": [dict(r) for r in rows]}
    except (sqlite3.Error, ServiceError):
        return {"items": [], "available": False}


def list_notifications(root: Path, limit: int = 50) -> dict[str, Any]:
    if not operations_db_available(root):
        return {"items": [], "available": False}
    limit = max(1, min(int(limit), 200))
    try:
        with closing(connect_operations_ro(root)) as conn:
            if "notification_deliveries" not in _operations_table_names(conn):
                return {"items": [], "available": False}
            rows = conn.execute(
                """
                SELECT d.delivery_id, d.channel, d.recipient_id, d.provider, d.status,
                       d.attempt_count, d.safe_error, d.submitted_at, d.accepted_at, d.created_at,
                       e.event_type, e.job
                FROM notification_deliveries d
                LEFT JOIN notification_events e ON e.event_id = d.event_id
                ORDER BY d.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items = [
            {**{k: v for k, v in dict(r).items() if k != "safe_error"},
             "safe_error": sanitize_error(r["safe_error"])}
            for r in rows
        ]
        return {"schema_version": "1", "available": True, "items": items}
    except (sqlite3.Error, ServiceError):
        return {"items": [], "available": False}
