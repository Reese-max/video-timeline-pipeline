"""Local Dashboard services. Reads never initialize stores; workers are explicit."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

import cost_tracker
import digests
import notifications
import operations as operations_schema
import pipeline
import scoring


class Unavailable(RuntimeError):
    def __init__(self, state="migration_required"):
        self.state = state
        super().__init__(state)


def initialize(root: Path) -> None:
    """Explicit maintenance only. No provider calls, queue claims, or RSS writes."""
    root.mkdir(parents=True, exist_ok=True)
    if any((root / name).is_symlink() for name in ("library.db", "operations.db", "jobs")):
        raise ValueError("Linked data stores are not supported")
    pipeline.build_index(root)
    with closing(notifications.get_operations_db(root)) as db:
        cost_tracker._init_cost_schema(db)
        scoring._init_scoring_schema(db)
        operations_schema.ensure_dashboard_schema(db)


def _db(root: Path, *, write=False):
    if not (root / "operations.db").is_file() or (root / "operations.db").is_symlink():
        raise Unavailable()
    db = sqlite3.connect((root / "operations.db").resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    if not operations_schema.dashboard_schema_ready(db):
        db.close()
        raise Unavailable()
    if not write:
        db.execute("PRAGMA query_only=ON")
    return db


def _hash(value):
    return notifications._json_hash(value)


def _job(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Invalid job ID")
    return value


def safe_source(value):
    """Only public platform links; discard credentials and signed URL parameters."""
    try:
        parsed = urlsplit(value or "")
        host = parsed.hostname or ""
        domains = ("youtube.com", "youtu.be", "facebook.com", "fb.watch", "instagram.com", "threads.com", "threads.net", "vimeo.com")
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return None
        if not any(host == d or host.endswith("." + d) for d in domains):
            return None
        return pipeline.public_url(value)
    except (ValueError, TypeError):
        return None


def _video(row):
    row = dict(row)
    row.pop("indexed_at", None)
    row["source"] = safe_source(row.get("source"))
    try:
        row["categories"] = json.loads(row["categories"] or "[]")
    except (ValueError, TypeError):
        row["categories"] = []
    row["score"] = None
    return row


def videos(root, **kwargs):
    try:
        result = pipeline.query_videos(root, readonly=True, **kwargs)
    except (RuntimeError, sqlite3.Error) as exc:
        raise Unavailable() from exc
    result["items"] = [_video(row) for row in result["items"]]
    if result["items"]:
        try:
            with closing(_db(root)) as db:
                ids = [item["job"] for item in result["items"]]
                placeholders = ",".join("?" for _ in ids)
                scores = db.execute(f"""SELECT job,importance,priority_score FROM (
                    SELECT job,importance,priority_score,ROW_NUMBER() OVER (PARTITION BY job ORDER BY scored_at DESC,score_id DESC) AS latest
                    FROM video_scores WHERE job IN ({placeholders})) WHERE latest=1""", ids)
                known = {row["job"]: {"importance": row["importance"], "priority_score": row["priority_score"]} for row in scores}
            for item in result["items"]:
                item["score"] = known.get(item["job"])
        except (Unavailable, sqlite3.Error):
            pass
    result.update(as_of=dt.datetime.now(dt.timezone.utc).isoformat(), scope="local_index", timezone=(kwargs.get("filters") or {}).get("timezone", "Asia/Taipei"))
    return result


def _artifact(root, job, name):
    base = root.resolve() / "jobs"
    path = base / _job(job) / name
    if (base.resolve() != base or path.parent.resolve() != base / job or path.is_symlink()
            or path.resolve().parent != base / job):
        raise ValueError("Unsafe artifact")
    if not path.exists():
        return None
    if path.stat().st_size > 2 * 1024 * 1024:
        raise Unavailable("partial")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable("partial") from exc


def _public_provenance(value):
    if isinstance(value, dict):
        return {key: _public_provenance(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_provenance(item) for item in value]
    return pipeline.sanitize_public_urls(value) if isinstance(value, str) else value


def normalize_hit(hit):
    hit = dict(hit)
    source = safe_source(hit.get("source"))
    timestamp = hit.get("timestamp")
    whole = hit.get("kind") in {"title", "summary"}
    if whole:
        timestamp = None
    hit.update(source=source, timestamp=timestamp,
               time_label="影片層級" if whole else "時間碼不可靠" if timestamp is None else pipeline.format_timestamp(timestamp),
               source_link=pipeline.source_link(source, timestamp) if source else None)
    for field in ("source_metadata", "metadata"):
        if field in hit:
            hit[field] = _public_provenance(hit[field])
    return hit


def _query(query, limit):
    if not isinstance(query, str) or not query.strip() or len(query) > 500 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Query must be 1–500 characters; limit must be 1–100")


def search(root, query, limit=20, filters=None):
    _query(query, limit)
    pipeline.library_filter_sql(filters, events=True)
    try:
        hits = pipeline.search_index(root, query, limit, readonly=True, filters=filters)
    except (RuntimeError, sqlite3.Error) as exc:
        raise Unavailable() from exc
    return {"items": [normalize_hit(h) for h in hits], "mode": "fts", "state": "ready" if hits else "empty"}


def video_detail(root, job, page=1, page_size=50, kind=""):
    _job(job)
    rows = videos(root, filters={"job": job}, page=page, page_size=page_size)
    # The video itself is independent of the evidence page.
    video = videos(root, filters={"job": job})["items"]
    if not video:
        raise LookupError("Unknown job")
    summary = _artifact(root, job, "summary.json")
    where, params = pipeline.library_filter_sql({"job": job, "kind": kind}, events=True)
    with closing(pipeline.library_read_connection(root)) as db:
        db.row_factory = sqlite3.Row
        base = "FROM events JOIN videos ON videos.job=events.job LEFT JOIN video_catalog c ON c.job=videos.job WHERE " + where
        total = db.execute("SELECT count(*) " + base, params).fetchone()[0]
        evidence = db.execute("SELECT videos.source, events.* " + base + " ORDER BY events.rowid LIMIT ? OFFSET ?",
                              [*params, page_size, (page - 1) * page_size]).fetchall()
    try:
        score = scoring.explain_score(root, job, readonly=True)
    except (RuntimeError, sqlite3.Error):
        score = {"status": "unscored"}
    related = videos(root, filters={"category": video[0]["categories"][0]}, page_size=6)["items"] if video[0]["categories"] else []
    return {"video": video[0], "summary": summary, "score": score,
            "operations": {"tasks": history(root, "operation_tasks", job=job), "usage": history(root, "usage_costs", job=job)},
            "related": [r for r in related if r["job"] != job],
            "evidence": {"items": [normalize_hit(dict(r)) for r in evidence], "total": total,
                         "page": rows["page"], "page_size": page_size}}


def stats(root, window="today", timezone="Asia/Taipei"):
    if window not in {"today", "week"}:
        raise ValueError("Invalid window")
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, KeyError) as exc:
        raise ValueError("Invalid timezone") from exc
    now = dt.datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        start -= dt.timedelta(days=start.weekday())
    end = start + dt.timedelta(days=1 if window == "today" else 7)
    try:
        with closing(pipeline.library_read_connection(root)) as db:
            count, missing = db.execute("""SELECT sum(CASE WHEN julianday(c.first_completed_at)>=julianday(?)
                AND julianday(c.first_completed_at)<julianday(?) THEN 1 ELSE 0 END),
                sum(CASE WHEN videos.status='completed' AND c.first_completed_at IS NULL THEN 1 ELSE 0 END)
                FROM videos LEFT JOIN video_catalog c ON c.job=videos.job""", (start.isoformat(), end.isoformat())).fetchone()
    except (RuntimeError, sqlite3.Error) as exc:
        raise Unavailable() from exc
    scored, high = 0, None
    try:
        with closing(_db(root)) as db:
            path = (root / "library.db").resolve().as_uri() + "?mode=ro"
            db.execute("ATTACH DATABASE ? AS library", (path,))
            scored, high = db.execute("""SELECT count(*),sum(CASE WHEN importance>=8.5 THEN 1 ELSE 0 END) FROM (
                SELECT job,importance,ROW_NUMBER() OVER (PARTITION BY job ORDER BY scored_at DESC,score_id DESC) AS latest
                FROM video_scores) scores JOIN library.video_catalog c ON c.job=scores.job
                WHERE latest=1 AND julianday(c.first_completed_at)>=julianday(?) AND julianday(c.first_completed_at)<julianday(?)""",
                (start.isoformat(), end.isoformat())).fetchone()
    except (Unavailable, sqlite3.Error):
        pass
    operations_data = operations(root)
    return {"completed": count or 0, "unknown_completion_time": missing or 0, "high_importance": (high or 0) if scored else None,
            "scored": scored, "scoring_state": "partial" if scored and scored < (count or 0) else "ready" if scored else "unscored", "window": {"from": start.isoformat(), "to": end.isoformat()},
            "timezone": timezone, "as_of": now.isoformat(), "scope": "local_index_and_operations",
            "counts": operations_data["counts"], "costs": costs(root),
            "video_filter": {"date_field": "first_completed_at", "timezone": timezone, "date_from": start.date().isoformat(),
                             "date_to": (end - dt.timedelta(days=1)).date().isoformat()}}


def costs(root, period="daily"):
    if period not in {"daily", "monthly"}:
        raise ValueError("Invalid period")
    try:
        report = cost_tracker.get_cost_report(root, period, readonly=True)
        budget = cost_tracker.get_budget_status(root, readonly=True)
    except (RuntimeError, sqlite3.Error):
        return {"state": "unavailable", "actual": None, "estimated": None, "unknown": None, "scope": "local_program_ledger"}
    providers = report["providers"]
    return {"state": "ready" if providers else "unavailable", "scope": "local_program_ledger",
            "as_of": dt.datetime.now(dt.timezone.utc).isoformat(), "report": report, "budget": budget,
            "actual": sum(p["actual_usd"] for p in providers if p["actual_usd"] is not None) if any(p["actual_usd"] is not None for p in providers) else None,
            "estimated": report["grand_total_estimated_usd"] if providers else None,
            "unknown": sum(p["unknown_count"] for p in providers) if providers else None,
            "confirmed_included": sum(p["confirmed_included_count"] for p in providers) if providers else None,
            "outstanding_reservations": budget["outstanding_usd"], "billing_complete": False}


def history(root, table, page=1, page_size=50, *, job=None):
    # Only these columns may leave the server. Never select safe_payload, payload_json or provider errors.
    columns = {"operation_tasks": "task_id,type,job,status,checkpoint,attempts,created_at,finished_at,safe_error",
               "usage_costs": "cost_id,job,operation,provider,status,cost_status,created_at",
               "notification_deliveries": "delivery_id,event_id,channel,status,attempt_count,created_at,accepted_at",
               "digest_runs": "digest_id,type,revision,status,candidate_count,selected_count,deferred_count,created_at",
               "web_digest_drafts": "digest_id,digest_type,revision,status,created_at"}
    if table not in columns or not 1 <= page <= 100000 or not 1 <= page_size <= 100:
        raise ValueError("Invalid history query")
    if job is not None and table not in {"operation_tasks", "usage_costs"}:
        raise ValueError("Invalid history filter")
    where, params = (" WHERE job=?", [_job(job)]) if job is not None else ("", [])
    try:
        with closing(_db(root)) as db:
            total = db.execute(f"SELECT count(*) FROM {table}" + where, params).fetchone()[0]
            rows = db.execute(f"SELECT {columns[table]} FROM {table}" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                              [*params, page_size, (page - 1) * page_size]).fetchall()
        return {"state": "ready" if rows else "empty", "items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}
    except (Unavailable, sqlite3.Error):
        return {"state": "unavailable", "items": [], "total": None}


def operations(root):
    try:
        state = pipeline.load_rss_state(root)
        rss = {"state": "ready", "source": "rss/state.json", "updated_at": state.get("updated_at"),
               "items": [{"id": i.get("id"), "job": i.get("job"), "status": i.get("status"), "attempts": i.get("attempts")}
                         for i in state["items"][:100]]}
    except (RuntimeError, OSError):
        rss = {"state": "unavailable", "source": "rss/state.json", "items": []}
    try:
        with closing(_db(root)) as db:
            counts = dict(db.execute("SELECT status,count(*) FROM operation_tasks GROUP BY status"))
        health = cost_tracker.get_provider_health(root, readonly=True)
        for provider in health["providers"]:
            provider["status"] = "observed_samples"
        failed = cost_tracker.get_failed_list(root, readonly=True)
        for item in failed:
            item["safe_error"] = "operation_failed_or_budget_blocked"
    except (Unavailable, RuntimeError, sqlite3.Error):
        counts, health, failed = {}, {"state": "unavailable", "providers": []}, []
    return {"rss": rss, "counts": counts, "providers": health, "failed": failed,
            "tasks": history(root, "operation_tasks"), "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scope": "dashboard_tasks_and_original_rss", "cache_savings_usd": None}


def _recipient():
    # Dashboard mail deliberately accepts configured email recipients only, never a URL from content.
    value = os.getenv("EMAIL_TO", "").strip()
    if value and (len(value) > 320 or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+", value)):
        raise ValueError("Configure a single EMAIL_TO address")
    return value


def _provider():
    name = os.getenv("EMAIL_PROVIDER", "smtp").strip().lower() or "smtp"
    if name not in {"smtp", "resend", "fake"}:
        raise ValueError("Dashboard digest requires an email provider")
    return name


def _evidence_hash(root, selected):
    return _hash([{"job": i["job"], "manifest": _artifact(root, i["job"], "manifest.json"),
                   "summary": _artifact(root, i["job"], "summary.json")} for i in selected])


def preview_digest(root, digest_type="daily", date=None, *, persist=False, new_revision=False):
    if digest_type not in {"daily", "weekly"}:
        raise ValueError("Invalid digest type")
    timezone = os.getenv("DIGEST_TIMEZONE", "Asia/Taipei")
    ZoneInfo(timezone)
    start, end, label = digests.compute_digest_window(digest_type, date, timezone)
    jobs = root / "jobs"
    if jobs.is_symlink() or (jobs.exists() and any(p.is_symlink() for p in jobs.iterdir())):
        raise ValueError("Linked artifacts are not supported")
    # Validate trust/size boundaries before handing files to the existing digest selector.
    if jobs.exists():
        for folder in jobs.iterdir():
            if folder.is_dir():
                _artifact(root, folder.name, "manifest.json")
                _artifact(root, folder.name, "summary.json")
    candidates = digests.collect_digest_candidates(root, start, end, include_chapters=False)
    for candidate in candidates:
        candidate["url"] = safe_source(candidate.get("url")) or ""
    payload = digests.build_digest_payload(candidates, start, end, label, digest_type)
    recipient, provider = _recipient(), _provider()
    payload["built_at"] = "frozen_at_confirmation"
    payload.pop("rendered_payload_hash", None)
    evidence_hash = _evidence_hash(root, payload["selected_items"])
    content_hash = _hash({"payload": payload, "recipient": recipient, "provider": provider, "evidence": evidence_hash})
    draft = {"digest_id": content_hash[:32], "revision": 1, "payload_hash": content_hash,
             "recipient": recipient or None, "provider": provider, "timezone": timezone,
             "payload": payload, "includes_immediate_notifications": "unknown", "state": "draft"}
    if persist:
        with closing(_db(root, write=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest_id,revision FROM web_digest_drafts WHERE payload_hash=? ORDER BY revision DESC LIMIT 1", (content_hash,)).fetchone()
            if existing and not new_revision:
                draft.update(digest_id=existing["digest_id"], revision=existing["revision"])
                return draft
            revision = db.execute("SELECT COALESCE(max(revision),0)+1 FROM web_digest_drafts WHERE digest_type=? AND window_start=? AND window_end=?",
                                  (digest_type, start.isoformat(), end.isoformat())).fetchone()[0]
            draft.update(digest_id=uuid.uuid4().hex, revision=revision)
            db.execute("INSERT INTO web_digest_drafts (digest_id,revision,payload_hash,digest_type,window_start,window_end,recipient,provider,payload,evidence_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (draft["digest_id"], revision, content_hash, digest_type, start.isoformat(), end.isoformat(), recipient, provider,
                        json.dumps(payload, ensure_ascii=False), evidence_hash, dt.datetime.now(dt.timezone.utc).isoformat()))
    return draft


def _confirmed_draft(root, payload):
    with closing(_db(root)) as db:
        row = db.execute("SELECT * FROM web_digest_drafts WHERE digest_id=?", (payload.get("digest_id"),)).fetchone()
    if not row or row["revision"] != payload.get("revision") or row["payload_hash"] != payload.get("payload_hash"):
        raise ValueError("Preview confirmation is stale")
    draft = dict(row)
    draft["payload"] = json.loads(draft["payload"])
    if (not draft["recipient"] or draft["recipient"] != _recipient() or draft["provider"] != _provider()
            or draft["evidence_hash"] != _evidence_hash(root, draft["payload"]["selected_items"])
            or draft["payload_hash"] != _hash({"payload": draft["payload"], "recipient": draft["recipient"],
                                                "provider": draft["provider"], "evidence": draft["evidence_hash"]})):
        raise ValueError("Preview content or configuration changed; preview again")
    return draft


def enqueue_task(root, kind, payload, request_key):
    if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_key) or not isinstance(payload, dict):
        raise ValueError("A bounded Idempotency-Key is required")
    if kind in {"ask", "search"}:
        if set(payload) - {"query", "limit", "mode", "filters"}:
            raise ValueError("Unknown task fields")
        payload = {"query": payload.get("query"), "limit": payload.get("limit", 20),
                   "mode": payload.get("mode", "fts" if kind == "ask" else "semantic"), "filters": payload.get("filters", {})}
        _query(payload["query"], payload["limit"])
        pipeline.library_filter_sql(payload["filters"], events=True)
        if payload["mode"] not in {"fts", "semantic", "hybrid"}:
            raise ValueError("Invalid search mode")
    elif kind == "digest.send":
        if set(payload) != {"digest_id", "revision", "payload_hash"}:
            raise ValueError("Confirm digest_id, revision and payload_hash")
        if (not isinstance(payload["digest_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", payload["digest_id"])
                or isinstance(payload["revision"], bool) or not isinstance(payload["revision"], int) or payload["revision"] < 1
                or not isinstance(payload["payload_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["payload_hash"])):
            raise ValueError("Invalid digest confirmation")
        _confirmed_draft(root, payload)
    elif kind == "rss.retry":
        if set(payload) != {"job", "allow_cloud"} or payload["allow_cloud"] is not True:
            raise ValueError("Explicit original cloud opt-in is required")
        _job(payload["job"])
        item = next((i for i in pipeline.load_rss_state(root)["items"] if i.get("id") == payload["job"]), None)
        pipeline.validate_rss_retry(item)
    else:
        raise ValueError("Unknown task type")
    digest = _hash({"type": kind, "payload": payload})
    with closing(_db(root, write=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        request = db.execute("SELECT * FROM operation_requests WHERE request_key=?", (request_key,)).fetchone()
        if request and request["payload_hash"] != digest:
            raise ValueError("Idempotency-Key was already used for different input")
        row = db.execute("SELECT task_id FROM operation_tasks WHERE dedup_key=? AND (type='digest.send' OR status IN ('queued','running','cancel_requested','unknown')) ORDER BY created_at DESC LIMIT 1", (digest,)).fetchone()
        task_id = request["task_id"] if request else row[0] if row else uuid.uuid4().hex
        if not row and not request:
            if db.execute("SELECT count(*) FROM operation_tasks WHERE status='queued'").fetchone()[0] >= 64:
                raise Unavailable("busy")
            db.execute("INSERT INTO operation_tasks (task_id,type,job,revision,safe_payload,dedup_key,status,created_at) VALUES (?,?,?,?,?,?,'queued',?)",
                       (task_id, kind, payload.get("job"), payload.get("revision"), json.dumps(payload, ensure_ascii=False), digest, time.time()))
        db.execute("INSERT OR IGNORE INTO operation_requests VALUES (?,?,?)", (request_key, digest, task_id))
    return get_task(root, task_id)


def get_task(root, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("Invalid task ID")
    with closing(_db(root)) as db:
        row = db.execute("SELECT task_id,type,job,revision,remote_operation_id,status,checkpoint,attempts,created_at,started_at,finished_at,safe_error,result FROM operation_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise LookupError("Unknown task")
    result = dict(row)
    result["result"] = json.loads(result["result"]) if result["result"] else None
    result["cancellation_note"] = "取消只停止後續步驟；已送出的請求可能繼續或已計費，不保證退費。"
    return result


def claim_task(root, owner, lease_seconds=60):
    now = time.time()
    with closing(_db(root, write=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE operation_tasks SET status=CASE WHEN checkpoint='submitted' THEN 'unknown'
            WHEN status='cancel_requested' THEN 'cancelled' ELSE 'queued' END, lease_token=NULL,
            safe_error=CASE WHEN checkpoint='submitted' THEN 'reconcile_remote_before_retry' ELSE NULL END
            WHERE status IN ('running','cancel_requested') AND lease_expires_at<=?""", (now,))
        # ponytail: one global task at a time; use per-provider limits if measured throughput requires them.
        if db.execute("SELECT 1 FROM operation_tasks WHERE status IN ('running','cancel_requested') LIMIT 1").fetchone():
            return None
        row = db.execute("SELECT * FROM operation_tasks WHERE status='queued' AND COALESCE(next_attempt_at,0)<=? ORDER BY created_at LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE operation_tasks SET status='running',lease_owner=?,lease_token=?,lease_expires_at=?,heartbeat=?,attempts=attempts+1,started_at=? WHERE task_id=?",
                   (owner, token, now + lease_seconds, now, now, row["task_id"]))
        return {**dict(row), "lease_token": token}


def checkpoint_task(root, task_id, token, checkpoint):
    if checkpoint != "submitted":
        raise ValueError("Invalid checkpoint")
    with closing(_db(root, write=True)) as db, db:
        return db.execute("UPDATE operation_tasks SET checkpoint=? WHERE task_id=? AND lease_token=? AND status='running' AND lease_expires_at>?",
                          (checkpoint, task_id, token, time.time())).rowcount == 1


def finish_task(root, task_id, token, status, result):
    if status not in {"succeeded", "failed", "blocked", "unknown", "cancelled"}:
        raise ValueError("Invalid terminal status")
    with closing(_db(root, write=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT type,status FROM operation_tasks WHERE task_id=?", (task_id,)).fetchone()
        if (task and task["type"] == "digest.send" and task["status"] == "cancel_requested"
                and result.get("status") == "retryable_not_accepted"):
            status, result = "cancelled", {**result, "status": "cancelled"}
        reason = result.get("state")
        safe_error = reason if reason in {"budget_blocked", "disabled", "migration_required", "confirmation_or_state_changed", "unknown", "error"} else status
        return db.execute("UPDATE operation_tasks SET status=?,result=?,finished_at=?,lease_token=NULL,safe_error=?,remote_operation_id=? WHERE task_id=? AND lease_token=? AND status IN ('running','cancel_requested') AND lease_expires_at>?",
                          (status, json.dumps(result, ensure_ascii=False), time.time(), safe_error if status in {"failed", "blocked", "unknown"} else None, result.get("remote_operation_id"),
                           task_id, token, time.time())).rowcount == 1


def cancel_task(root, task_id):
    get_task(root, task_id)
    with closing(_db(root, write=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT type,safe_payload FROM operation_tasks WHERE task_id=?", (task_id,)).fetchone()
        db.execute("UPDATE operation_tasks SET status=CASE WHEN status='running' THEN 'cancel_requested' ELSE 'cancelled' END WHERE task_id=? AND (status IN ('queued','running') OR (type='digest.send' AND status='failed'))", (task_id,))
        if task["type"] == "digest.send":
            payload = json.loads(task["safe_payload"])
            identity = f"web-digest:{payload['digest_id']}:{payload['revision']}:{payload['payload_hash']}"
            db.execute("UPDATE notification_deliveries SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE provider_idempotency_key=? AND status IN ('pending','retry_wait')", (identity,))
    return get_task(root, task_id)


def _cloud_search(root, payload, *, public=True):
    mode, query, limit, filters = (payload[k] for k in ("mode", "query", "limit", "filters"))
    if mode == "fts":
        hits = pipeline.search_index(root, query, limit, readonly=True, filters=filters)
        return [normalize_hit(hit) for hit in hits] if public else hits
    # Web never downloads/loads a local model. Cloud embedding is an explicit task with the original guard.
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise Unavailable("disabled")
    retrieved = pipeline.retrieve_evidence(root, query, limit, mode=mode,
        filters=pipeline.dashboard_search_filter(filters), embed_key=key,
        embed_model=pipeline.DEFAULT_EMBED_MODEL["openrouter"], strict=True)
    hits = retrieved["evidence"]
    return [normalize_hit(hit) for hit in hits] if public else hits


def _answer_preselected(root, payload, hits):
    """Keep raw source identity internal for verification, then publish safe references."""
    result = pipeline.answer_question(root, payload["query"], pipeline.minimax_subscription_key(),
        pipeline.minimax_model(), payload["limit"], evidence=hits,
        filters=pipeline.dashboard_search_filter(payload["filters"]))
    for field in ("citations", "evidence"):
        if field in result:
            result[field] = [normalize_hit(hit) for hit in result[field]]
    return result


def _require_answer_config():
    try:
        pipeline.minimax_subscription_key()
        pipeline.minimax_model()
    except RuntimeError as exc:
        raise Unavailable("disabled") from exc


def run_worker_once(root, *, provider=None):
    task = claim_task(root, f"worker-{os.getpid()}-{uuid.uuid4().hex}")
    if task is None:
        return None
    task_id, token = task["task_id"], task["lease_token"]
    payload = json.loads(task["safe_payload"])
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(10):
            with closing(_db(root, write=True)) as db, db:
                db.execute("UPDATE operation_tasks SET heartbeat=?,lease_expires_at=? WHERE task_id=? AND lease_token=? AND lease_expires_at>?",
                           (time.time(), time.time() + 60, task_id, token, time.time()))

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    status, result = "failed", {"state": "error"}
    try:
        with cost_tracker.cost_root_scope(root), pipeline.durable_transport_scope(
                lambda: checkpoint_task(root, task_id, token, "submitted")):
            if task["type"] == "ask" and payload["mode"] == "fts":
                evidence = _cloud_search(root, payload, public=False)
                if not evidence:
                    status, result = "succeeded", {"state": "insufficient_evidence", "answer": "找不到可引用的本機證據。", "citations": []}
                else:
                    _require_answer_config()
                    if not checkpoint_task(root, task_id, token, "submitted"):
                        status, result = "cancelled", {"state": "cancelled"}
                    else:
                        result = _answer_preselected(root, payload, evidence)
                        status = "succeeded"
            elif task["type"] in {"ask", "search"}:
                if task["type"] == "ask":
                    _require_answer_config()  # Do not buy embeddings for a locally disabled answer step.
                if not checkpoint_task(root, task_id, token, "submitted"):
                    status, result = "cancelled", {"state": "cancelled"}
                else:
                    hits = _cloud_search(root, payload, public=task["type"] != "ask")
                    result = {"items": hits, "state": "ready" if hits else "insufficient_evidence"}
                    if task["type"] == "ask":
                        if not checkpoint_task(root, task_id, token, "submitted"):
                            status, result = "cancelled", {"state": "cancelled", "prior_request_may_be_billed": True}
                            finish_task(root, task_id, token, status, result)
                            return get_task(root, task_id)
                        result = _answer_preselected(root, payload, hits)
                    status = "succeeded"
            elif task["type"] == "digest.send":
                draft = _confirmed_draft(root, payload)
                if checkpoint_task(root, task_id, token, "submitted"):
                    result = notifications.send_frozen_digest(root, draft, provider=provider,
                        before_send=lambda: checkpoint_task(root, task_id, token, "submitted"))
                    status = {"blocked": "blocked", "unknown": "unknown", "cancelled": "cancelled", "accepted": "succeeded", "skipped": "succeeded"}.get(result["status"], "failed")
                else:
                    status, result = "cancelled", {"state": "cancelled"}
            elif task["type"] == "rss.retry":
                with pipeline.rss_writer_lock(root):
                    state = pipeline.load_rss_state(root)
                    item = next((i for i in state["items"] if i.get("id") == payload["job"]), None)
                    pipeline.validate_rss_retry(item)
                    if checkpoint_task(root, task_id, token, "submitted"):
                        pipeline.requeue_rss_item(root, payload["job"], preserve_retry_budget=True)
                        status, result = "succeeded", {"status": "queued", "job": payload["job"], "scope": "original_rss"}
                    else:
                        status, result = "cancelled", {"state": "cancelled"}
    except cost_tracker.BudgetBlockedError:
        status, result = "blocked", {"state": "budget_blocked"}
    except Unavailable as exc:
        status, result = "blocked", {"state": exc.state}
    except pipeline.ExecutionCancelled:
        status, result = "cancelled", {"state": "cancelled", "prior_request_may_be_billed": True}
    except ValueError:
        if get_task(root, task_id)["checkpoint"] == "submitted":
            status, result = "unknown", {"state": "unknown", "action": "reconcile_remote_before_retry"}
        else:
            status, result = "blocked", {"state": "confirmation_or_state_changed"}
    except Exception:
        # After checkpoint, the transport may have accepted a request. Never auto-replay it.
        status, result = "unknown", {"state": "unknown", "action": "reconcile_remote_before_retry"}
    finally:
        stopped.set()
        thread.join(timeout=2)
    finish_task(root, task_id, token, status, result)
    return get_task(root, task_id)
