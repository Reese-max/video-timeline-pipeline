"""Operations foundation layer for operations.db (Issue #7 roadmap, F0).

Shared primitives every feature work package builds on:

- F0.1: a single versioned migration registry (``schema_migrations``) instead
  of uncoordinated ``CREATE TABLE IF NOT EXISTS`` scattered across services,
  and a read-only access contract — readers never run DDL and an
  uninitialized store reports ``unavailable``/``migration_required``.
- F0.2: a stable ``library_id`` per data collection (generated once, kept in
  ``library_meta``, reused across restarts and preserved when the root is
  moved or copied), plus the ``processed_revisions`` completion registry.

``analysis_revision`` is derived from canonical analysis content — index
times, run times and absolute root paths never enter the hash, so rerunning
identical content yields the identical revision and never becomes a newly
notifiable video.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import urllib.parse
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


FOUNDATION_SCHEMA_VERSION = 2

# Analysis artifacts recorded by the registry. Rendered outputs
# (notes.md, knowledge.md, *.srt) and bookkeeping (source.json,
# manifest.json) are excluded on purpose: they are regenerated from content
# or change every run without meaning new analysis.
CONTENT_ARTIFACTS = (
    "transcript",
    "transcript_raw",
    "visual",
    "timeline",
    "summary",
    "steipete_slides",
)

REVISION_ARTIFACTS = ("transcript", "visual", "timeline", "summary")
REVISION_FORMAT = "f0-analysis-revision-2"

_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "f0_foundation",
        (
            """
            CREATE TABLE IF NOT EXISTS library_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_revisions (
                revision_id TEXT PRIMARY KEY,
                library_id TEXT NOT NULL,
                job TEXT NOT NULL,
                analysis_revision TEXT NOT NULL,
                artifact_hashes TEXT NOT NULL DEFAULT '{}',
                first_completed_at TEXT,
                eligible_at TEXT,
                backfill INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(library_id, job, analysis_revision)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_processed_job
                ON processed_revisions(library_id, job)
            """,
        ),
    ),
    (
        2,
        "notification_and_digest_schema",
        (
            """
            CREATE TABLE IF NOT EXISTS notification_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                library_id TEXT,
                job TEXT,
                analysis_revision TEXT,
                payload_version TEXT DEFAULT '1',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                dedup_key TEXT UNIQUE NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                provider_idempotency_key TEXT,
                provider_message_id TEXT,
                attempt_count INTEGER DEFAULT 0,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                next_attempt_at TEXT,
                safe_error TEXT,
                submitted_at TEXT,
                accepted_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES notification_events(event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                attempt_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                request_id TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                outcome TEXT NOT NULL,
                safe_error TEXT,
                FOREIGN KEY(delivery_id) REFERENCES notification_deliveries(delivery_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS digest_runs (
                digest_id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                type TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                timezone TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                revision INTEGER DEFAULT 1,
                selection_snapshot TEXT NOT NULL,
                rendered_payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_count INTEGER DEFAULT 0,
                selected_count INTEGER DEFAULT 0,
                deferred_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS digest_items (
                digest_id TEXT NOT NULL,
                job TEXT NOT NULL,
                analysis_revision TEXT,
                rank INTEGER NOT NULL,
                reason TEXT,
                disposition TEXT NOT NULL,
                PRIMARY KEY(digest_id, job),
                FOREIGN KEY(digest_id) REFERENCES digest_runs(digest_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries
                ON notification_deliveries(event_id, channel, recipient_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_runs
                ON digest_runs(schedule_id, type, window_start, window_end, recipient_id, revision)
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_deliveries_status
                ON notification_deliveries(status, next_attempt_at)
            """,
        ),
    ),
)
 
 


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[tuple[int, str, tuple[str, ...]], ...] | list[tuple[int, str, tuple[str, ...]]],
) -> list[int]:
    """Apply pending migrations; each migration commits as one transaction.

    A failing migration rolls back cleanly — no half-applied schema and no
    registry row, so it can be retried after the defect is fixed. Statements
    run via ``conn.execute`` because Python sqlite3 implicitly commits any
    pending transaction before ``executescript``.
    """
    newly_applied: list[int] = []
    for version, name, statements in migrations:
        # BEGIN IMMEDIATE serializes concurrent initializers and the applied
        # set is re-read inside the transaction — a second writer that loses
        # the race sees the committed row instead of dying on the PRIMARY
        # KEY conflict. Explicit BEGIN is also required for atomicity: under
        # sqlite3's legacy isolation_level, DDL would otherwise autocommit
        # and a mid-migration failure would leave a half-applied schema.
        # Callers must not hold an open transaction on this connection.
        conn.execute("BEGIN IMMEDIATE")
        try:
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if already:
                conn.execute("COMMIT")
                continue
            for sql in statements:
                conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _utc_now_iso()),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        newly_applied.append(version)
    return newly_applied


def ensure_foundation_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap the migration registry and apply foundation migrations."""
    with conn:
        conn.execute(_BOOTSTRAP_DDL)
    apply_migrations(conn, MIGRATIONS)


def get_or_create_library_id(conn: sqlite3.Connection) -> str:
    """Return this collection's stable library_id, generating it once.

    The id lives in ``library_meta`` so it survives restarts and is preserved
    when the root directory is moved or copied. Two racing writers converge
    on the same stored value via INSERT OR IGNORE + re-read.
    """
    row = conn.execute(
        "SELECT value FROM library_meta WHERE key = 'library_id'"
    ).fetchone()
    if row:
        return str(row[0])
    candidate = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO library_meta (key, value) VALUES ('library_id', ?)",
            (candidate,),
        )
    row = conn.execute(
        "SELECT value FROM library_meta WHERE key = 'library_id'"
    ).fetchone()
    return str(row[0]) if row else candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_job_artifacts(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Content fingerprints of the analysis artifacts named in the manifest.

    Revision artifacts use canonical JSON without cache counters or local
    image paths. Raw provider/extractor artifacts retain byte checksums for
    provenance but do not define the revision. Missing files are skipped
    here; verified completion requires all declared artifacts below.
    """
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object")
    hashes: dict[str, str] = {}
    for name in CONTENT_ARTIFACTS:
        value = artifacts.get(name)
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve()
        # Never hash bytes outside this root — absolute paths recorded before
        # a library move are provenance, not content belonging to this store.
        if not resolved.is_relative_to(root.resolve()):
            continue
        if resolved.is_file():
            if name not in REVISION_ARTIFACTS:
                hashes[name] = _sha256_file(resolved)
                continue
            content = json.loads(resolved.read_text(encoding="utf-8"))
            expected_type = dict if name in {"transcript", "summary"} else list
            if not isinstance(content, expected_type):
                raise ValueError(f"invalid {name} artifact")
            if isinstance(content, list) and any(not isinstance(item, dict) for item in content):
                raise ValueError(f"invalid {name} item")
            if name == "transcript":
                content.pop("cache", None)
            elif name == "visual":
                content = [{k: v for k, v in item.items() if k != "source_image"} for item in content]
            elif name == "timeline":
                for event in content:
                    if event.get("type") == "visual" and isinstance(event.get("data"), dict):
                        event["data"].pop("source_image", None)
            material = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            hashes[name] = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return hashes


def verified_artifact_hashes(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Reject missing/out-of-root artifacts and unusable completed summaries."""
    hashes = hash_job_artifacts(root, manifest)
    artifacts = manifest.get("artifacts", {})
    expected = {name for name in CONTENT_ARTIFACTS if artifacts.get(name) is not None}
    if "summary" not in hashes or expected != hashes.keys():
        raise ValueError("completed analysis artifacts are missing or outside the root")
    summary_path = root / artifacts["summary"]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not summary or summary.get("status") == "skipped":
        raise ValueError("completed summary is empty or skipped")
    return hashes


def compute_analysis_revision(
    manifest: dict[str, Any], artifact_hashes: dict[str, str]
) -> str:
    """Deterministic revision id; raw request/extractor metadata is provenance."""
    material = {
        "format": REVISION_FORMAT,
        "schema_version": manifest.get("schema_version"),
        "artifacts": {k: artifact_hashes[k] for k in sorted(artifact_hashes) if k in REVISION_ARTIFACTS},
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def get_processed_revision(
    conn: sqlite3.Connection, library_id: str, job: str, analysis_revision: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM processed_revisions
        WHERE library_id = ? AND job = ? AND analysis_revision = ?
        """,
        (library_id, job, analysis_revision),
    ).fetchone()


def register_processed_revision(
    conn: sqlite3.Connection,
    *,
    library_id: str,
    job: str,
    analysis_revision: str,
    artifact_hashes: dict[str, str],
    first_completed_at: str | None,
    eligible_at: str | None,
    backfill: bool,
    status: str,
) -> tuple[sqlite3.Row, bool]:
    """Insert the revision row once. Caller owns the transaction.

    Requires a connection with ``row_factory = sqlite3.Row`` (as returned by
    ``get_operations_db``). Returns (row, created): ``created`` is False when
    the same (library_id, job, analysis_revision) was already registered, and
    the returned row then carries the original first_completed_at/eligible_at.
    """
    revision_id = str(uuid.uuid4())
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO processed_revisions (
            revision_id, library_id, job, analysis_revision, artifact_hashes,
            first_completed_at, eligible_at, backfill, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            library_id,
            job,
            analysis_revision,
            json.dumps(artifact_hashes, ensure_ascii=False, sort_keys=True),
            first_completed_at,
            eligible_at,
            1 if backfill else 0,
            status,
            _utc_now_iso(),
        ),
    )
    row = get_processed_revision(conn, library_id, job, analysis_revision)
    return row, bool(cur.rowcount)


def _db_path(root: Path) -> Path:
    return root / "operations.db"


def open_readonly(root: Path) -> sqlite3.Connection | None:
    """Open operations.db read-only; None when the store does not exist.

    Read interfaces never create the database or run migrations (F0.1).
    """
    path = _db_path(root)
    if not path.is_file():
        return None
    uri = f"file:{urllib.parse.quote(str(path.absolute()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def foundation_status(root: Path) -> dict[str, Any]:
    """Report foundation readiness without mutating the store.

    A corrupt or unreadable store reports ``unavailable`` rather than
    raising — read paths never fail closed onto write paths.
    """
    conn = None
    try:
        conn = open_readonly(root)
        if conn is None:
            return {"status": "unavailable", "library_id": None, "schema_version": None}
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "schema_migrations", "library_meta", "processed_revisions",
            "notification_events", "notification_deliveries", "delivery_attempts",
            "digest_runs", "digest_items",
        }
        if not required.issubset(tables):
            return {
                "status": "migration_required",
                "library_id": None,
                "schema_version": None,
            }
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        if int(version[0] or 0) < FOUNDATION_SCHEMA_VERSION:
            return {"status": "migration_required", "library_id": None, "schema_version": int(version[0] or 0)}
        library = conn.execute(
            "SELECT value FROM library_meta WHERE key = 'library_id'"
        ).fetchone()
        return {
            "status": "ready",
            "library_id": library[0] if library else None,
            "schema_version": int(version[0] or 0),
        }
    except (sqlite3.Error, OSError) as exc:
        return {
            "status": "unavailable",
            "library_id": None,
            "schema_version": None,
            "error": str(exc),
        }
    finally:
        if conn is not None:
            conn.close()


def load_eligibility_index(root: Path) -> dict[str, dict[str, Any]] | None:
    """Registry eligibility, keyed per job, for read-only consumers.

    Returns a mapping ``job -> {"revisions": {revision: eligible_at},
    "latest": eligible_at}`` where "latest" prefers non-backfill rows ordered
    by first_completed_at — a reconciliation sweep registering old content
    never shadows a genuinely newer completion.

    ``None`` means the registry exists but could not be read; consumers that
    must not send historical content should fail closed. ``{}`` means no
    store or an empty registry — callers fall back to manifest fields. A
    present-but-None eligible_at means "registered, not eligible" — callers
    must not substitute a fabricated timestamp (F0.2: 預設不寄歷史內容).
    """
    conn = None
    try:
        conn = open_readonly(root)
        if conn is None:
            return {}
        rows = conn.execute(
            """
            SELECT job, analysis_revision, eligible_at,
                   first_completed_at, backfill, created_at
            FROM processed_revisions
            """
        ).fetchall()
    except (sqlite3.Error, OSError):
        return None
    finally:
        if conn is not None:
            conn.close()

    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        job = str(row["job"])
        entry = index.setdefault(job, {"revisions": {}, "latest": None, "_rank": (-1, "", "")})
        entry["revisions"][str(row["analysis_revision"])] = row["eligible_at"]
        # Non-backfill rows first (real completions beat late-discovered old
        # content), then completion time, then registration order.
        rank = (
            0 if row["backfill"] else 1,
            row["first_completed_at"] or "",
            row["created_at"] or "",
        )
        if rank > entry["_rank"]:
            entry["_rank"] = rank
            entry["latest"] = row["eligible_at"]
    for entry in index.values():
        entry.pop("_rank", None)
    return index


def reconcile_processed_revisions(root: Path) -> dict[str, int]:
    """Sweep jobs/*/manifest.json for completed runs never registered.

    This is the F0.2 reconciliation path for registrations the completion
    hook missed. Backfilled rows carry ``backfill=1``; when the manifest has
    no recorded ``eligible_at`` the first_completed_at is left NULL rather
    than fabricated, and the row stays ineligible for notification.
    """
    if not root.is_dir():
        return {"scanned": 0, "registered": 0, "skipped": 0}

    from notifications import _insert_event, get_operations_db

    scanned = registered = skipped = 0
    with closing(get_operations_db(root)) as conn:
        library_id = get_or_create_library_id(conn)
        jobs_dir = root / "jobs"
        manifests = (
            sorted(jobs_dir.glob("*/manifest.json")) if jobs_dir.is_dir() else []
        )
        for manifest_path in manifests:
            scanned += 1
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or manifest.get("status") != "completed":
                    skipped += 1
                    continue
                artifact_hashes = verified_artifact_hashes(root, manifest)
                source = manifest.get("source", {})
                if not isinstance(source, dict):
                    raise ValueError("source must be an object")
            except (OSError, ValueError):
                skipped += 1
                continue
            revision = compute_analysis_revision(manifest, artifact_hashes)
            recorded_revision = manifest.get("analysis_revision")
            # Trust a completion timestamp only when it belongs to these bytes.
            first_completed = (
                manifest.get("eligible_at") if recorded_revision == revision else None
            )
            with conn:
                _, created = register_processed_revision(
                    conn,
                    library_id=library_id,
                    job=manifest_path.parent.name,
                    analysis_revision=str(revision),
                    artifact_hashes=artifact_hashes,
                    first_completed_at=first_completed,
                    eligible_at=first_completed,
                    backfill=True,
                    status="completed",
                )
                job = manifest_path.parent.name
                _insert_event(
                    conn, "video.completed", library_id, job, revision,
                    {"job": job, "title": source.get("title", job), "url": source.get("url", ""),
                     "analysis_revision": revision, "created_at": _utc_now_iso(), "backfill": True},
                    f"video.completed:{job}:{revision}",
                )
            if created:
                registered += 1
    return {"scanned": scanned, "registered": registered, "skipped": skipped}
