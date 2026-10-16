"""Shared operations.db migrations and verified completion identity (F0, #7).

The ledger survives search rebuilds. Readers never initialize it; callers own
short write transactions and keep provider requests outside those transactions.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any


FOUNDATION_SCHEMA_VERSION = 4
CONTENT_ARTIFACTS = ("transcript", "timeline", "summary", "visual")
REVISION_FORMAT = "f0-analysis-revision-1"

MIGRATIONS = (
    (1, 'f0_foundation', (
        """CREATE TABLE IF NOT EXISTS library_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS processed_revisions (
        library_id TEXT NOT NULL, job TEXT NOT NULL, analysis_revision TEXT NOT NULL,
        artifact_hashes TEXT NOT NULL, first_completed_at TEXT, eligible_at TEXT,
        backfill INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
        PRIMARY KEY (library_id, job, analysis_revision))""",
    )),
    (2, 'notifications_and_digests', (
        """CREATE TABLE IF NOT EXISTS notification_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    library_id TEXT,
    job TEXT,
    analysis_revision TEXT,
    payload_version TEXT DEFAULT '1',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dedup_key TEXT UNIQUE NOT NULL
)""",
        """CREATE TABLE IF NOT EXISTS notification_deliveries (
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
)""",
        """CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    request_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL,
    safe_error TEXT,
    FOREIGN KEY(delivery_id) REFERENCES notification_deliveries(delivery_id)
)""",
        """CREATE TABLE IF NOT EXISTS digest_runs (
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
)""",
        """CREATE TABLE IF NOT EXISTS digest_items (
    digest_id TEXT NOT NULL,
    job TEXT NOT NULL,
    analysis_revision TEXT,
    rank INTEGER NOT NULL,
    reason TEXT,
    disposition TEXT NOT NULL,
    PRIMARY KEY(digest_id, job),
    FOREIGN KEY(digest_id) REFERENCES digest_runs(digest_id)
)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries ON notification_deliveries(event_id, channel, recipient_id)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_runs ON digest_runs(schedule_id, type, window_start, window_end, recipient_id, revision)""",
        """CREATE INDEX IF NOT EXISTS ix_deliveries_status ON notification_deliveries(status, next_attempt_at)""",
    )),
    (3, 'costs_and_budgets', (
        """CREATE TABLE IF NOT EXISTS usage_costs (
    cost_id TEXT PRIMARY KEY,
    logical_operation_id TEXT,
    library_id TEXT,
    job TEXT,
    analysis_revision TEXT,
    provider TEXT NOT NULL,
    account_scope TEXT DEFAULT 'default',
    model TEXT,
    operation TEXT NOT NULL,
    attempt_no INTEGER DEFAULT 1,
    request_units INTEGER DEFAULT 0,
    response_units INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    latency_ms REAL,
    cache_hit INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    safe_error TEXT,
    provider_request_id TEXT,
    billing_basis TEXT,
    pricing_version TEXT DEFAULT '1',
    estimated_micro_usd INTEGER DEFAULT 0,
    actual_micro_usd INTEGER,
    cost_status TEXT NOT NULL,
    reservation_id TEXT,
    created_at TEXT NOT NULL
)""",
        """CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    account_scope TEXT DEFAULT 'default',
    period_id TEXT NOT NULL,
    max_micro_usd INTEGER NOT NULL,
    status TEXT NOT NULL,
    lease_token TEXT,
    sent_at TEXT,
    provider_operation_id TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL
)""",
        """CREATE INDEX IF NOT EXISTS ix_costs_job ON usage_costs(job, provider)""",
        """CREATE INDEX IF NOT EXISTS ix_costs_period ON usage_costs(created_at, provider)""",
        """-- Outstanding reservations are selected by the configured period
-- window, so they are indexed by the same columns the filter uses.
-- period_id is kept as a stored audit label (reporting, dedup key)
-- but is no longer a predicate, so ix_budget_period is not created.
CREATE INDEX IF NOT EXISTS ix_budget_reserved_window
    ON budget_reservations(account_scope, created_at, status)""",
    )),
    (4, 'scoring', (
        """CREATE TABLE IF NOT EXISTS video_scores (
    score_id TEXT PRIMARY KEY,
    library_id TEXT,
    job TEXT NOT NULL,
    analysis_revision TEXT NOT NULL,
    current_input_hash TEXT NOT NULL,
    schema_version TEXT DEFAULT '1',
    prompt_version TEXT DEFAULT '1',
    model_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    knowledge_cutoff TEXT,
    retrieval_version TEXT DEFAULT '1',
    status TEXT NOT NULL,
    importance REAL,
    relevance REAL,
    novelty REAL,
    novelty_status TEXT NOT NULL,
    evidence_confidence REAL,
    priority_score REAL,
    reasons_json TEXT,
    claims_json TEXT,
    raw_output TEXT,
    cache_key TEXT UNIQUE,
    scored_at TEXT NOT NULL
)""",
        """CREATE TABLE IF NOT EXISTS score_decisions (
    decision_id TEXT PRIMARY KEY,
    score_id TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    priority REAL,
    decision TEXT NOT NULL,
    reasons TEXT,
    decided_at TEXT NOT NULL,
    FOREIGN KEY(score_id) REFERENCES video_scores(score_id)
)""",
        """CREATE INDEX IF NOT EXISTS ix_scores_job ON video_scores(job, analysis_revision)""",
    )),
)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def apply_migrations(conn: sqlite3.Connection, migrations) -> list[int]:
    """Serialize initializers; each migration's DDL and receipt commit together."""
    applied = []
    for version, name, statements in migrations:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                continue
            for statement in statements:
                conn.execute(statement)
            if version == 3:
                # Adopt pre-link cost ledgers without replacing their audit rows.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_costs)")}
                if "reservation_id" not in columns:
                    conn.execute("ALTER TABLE usage_costs ADD COLUMN reservation_id TEXT")
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, _utc_now_iso()))
        applied.append(version)
    return applied


def ensure_foundation_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if versions - set(range(1, FOUNDATION_SCHEMA_VERSION + 1)):
            raise RuntimeError("Unsupported operations schema; use the matching application version")
    apply_migrations(conn, MIGRATIONS)
    get_or_create_library_id(conn)


def get_or_create_library_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM library_meta WHERE key='library_id'").fetchone()
    if row is None:
        with conn:
            conn.execute("INSERT OR IGNORE INTO library_meta VALUES ('library_id', ?)", (str(uuid.uuid4()),))
        row = conn.execute("SELECT value FROM library_meta WHERE key='library_id'").fetchone()
    return str(row[0])


def _schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"schema_migrations", "library_meta", "processed_revisions", "notification_events",
                "notification_deliveries", "delivery_attempts", "digest_runs", "digest_items",
                "usage_costs", "budget_reservations", "video_scores", "score_decisions"}
    if not required.issubset(tables):
        return {"status": "migration_required"}
    versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    if versions != set(range(1, FOUNDATION_SCHEMA_VERSION + 1)):
        return {"status": "migration_required"}
    row = conn.execute("SELECT value FROM library_meta WHERE key='library_id'").fetchone()
    if row is None:
        return {"status": "migration_required"}
    return {"status": "ready", "schema_version": FOUNDATION_SCHEMA_VERSION, "library_id": row[0]}


def open_readonly(root: Path, *, require_current: bool = True) -> sqlite3.Connection | None:
    """No DDL, model downloads or file creation, including on missing/old stores."""
    path = root / "operations.db"
    if not path.is_file():
        return None
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        if require_current and _schema_status(conn)["status"] != "ready":
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        return None


def foundation_status(root: Path) -> dict[str, Any]:
    conn = open_readonly(root, require_current=False)
    if conn is None:
        return {"status": "unavailable"}
    with closing(conn):
        try:
            return _schema_status(conn)
        except sqlite3.Error:
            return {"status": "unavailable"}


def _content(value: Any) -> Any:
    """Remove runtime provenance, retaining timestamps within the actual evidence."""
    if isinstance(value, dict):
        return {key: _content(item) for key, item in value.items()
                if key not in {"cache", "request_seconds", "source_video", "minimax_file_id"}}
    if isinstance(value, list):
        return [_content(item) for item in value]
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def completion_snapshot(root: Path, job_dir: Path, manifest: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Validate declared local evidence; raw hashes audit bytes, revision binds content."""
    if manifest.get("status") != "completed":
        raise ValueError("Analysis is not completed")
    job_dir = job_dir.resolve()
    if job_dir.parent != (root / "jobs").resolve():
        raise ValueError("Job is outside this library")
    declarations = manifest.get("artifacts") or {}
    data, raw_hashes = {}, {}
    for name in CONTENT_ARTIFACTS:
        declaration = declarations.get(name)
        if name == "visual" and not declaration:
            continue
        if not isinstance(declaration, str) or not declaration:
            raise ValueError(f"Missing {name} artifact")
        path = Path(declaration.replace("\\", "/"))
        if not path.is_absolute():
            path = root / path
        if not path.resolve().is_relative_to(job_dir):
            raise ValueError("Artifact escapes its job")
        raw = path.read_bytes()
        data[name] = json.loads(raw)
        raw_hashes[name] = hashlib.sha256(raw).hexdigest()
    transcript, timeline, summary = (data[name] for name in ("transcript", "timeline", "summary"))
    if (not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list)
            or transcript.get("status") in {"skipped", "failed", "incomplete"}
            or (transcript.get("gap_check") or {}).get("remaining") or transcript.get("backfill_errors")
            or not isinstance(timeline, list) or not all(isinstance(row, dict) for row in timeline)
            or not isinstance(summary, dict) or not summary or "raw" in summary
            or summary.get("status") in {"skipped", "failed", "incomplete"}):
        raise ValueError("Incomplete analysis artifacts")
    if not any(isinstance(summary.get(key), str) and summary[key].strip()
               for key in ("short_summary", "brief", "summary")):
        raise ValueError("Missing summary text")
    for segment in transcript["segments"]:
        if (not isinstance(segment, dict)
                or not isinstance(segment.get("text"), str) or not segment["text"].strip()
                or not all(isinstance(segment.get(key), (int, float)) and not isinstance(segment[key], bool)
                           and math.isfinite(segment[key]) for key in ("start", "end"))
                or not 0 <= segment["start"] <= segment["end"]):
            raise ValueError("Invalid transcript segment")
    visual = data.get("visual", [])
    if not isinstance(visual, list):
        raise ValueError("Invalid visual artifact")
    if visual:
        from pipeline import usable_visual_observation, whole_video_observation
        if not all(usable_visual_observation(row) and whole_video_observation(row) for row in visual):
            raise ValueError("Incomplete visual evidence")
    elif (manifest.get("models") or {}).get("visual") or any(
            stream.get("codec_type") == "video" for stream in (manifest.get("video") or {}).get("streams", [])):
        raise ValueError("Missing visual evidence")
    data["visual"] = visual  # Missing and empty visual evidence are identical for audio/captions jobs.
    revision = _hash({"format": REVISION_FORMAT, "schema_version": manifest.get("schema_version"),
                      "models": {key: value for key, value in (manifest.get("models") or {}).items() if value is not None},
                      "source_sha256": manifest.get("source_sha256"),
                      "content": _content(data)})
    return revision, raw_hashes


def register_processed_revision(conn: sqlite3.Connection, *, library_id: str, job: str,
                                analysis_revision: str, artifact_hashes: dict[str, str],
                                first_completed_at: str | None, eligible_at: str | None,
                                backfill: bool, status: str = "completed") -> tuple[sqlite3.Row, bool]:
    """Caller owns the transaction; identical reruns retain the first receipt."""
    cur = conn.execute(
        """INSERT INTO processed_revisions
           (library_id, job, analysis_revision, artifact_hashes, first_completed_at, eligible_at, backfill, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(library_id, job, analysis_revision) DO NOTHING""",
        (library_id, job, analysis_revision, json.dumps(artifact_hashes, sort_keys=True),
         first_completed_at, eligible_at, int(backfill), status))
    row = conn.execute("SELECT * FROM processed_revisions WHERE library_id=? AND job=? AND analysis_revision=?",
                       (library_id, job, analysis_revision)).fetchone()
    return row, bool(cur.rowcount)


def load_eligibility_index(root: Path) -> dict[tuple[str, str], str | None] | None:
    conn = open_readonly(root)
    if conn is None:
        return None if (root / "operations.db").exists() else {}
    with closing(conn):
        return {(row["job"], row["analysis_revision"]): row["eligible_at"]
                for row in conn.execute("SELECT job, analysis_revision, eligible_at FROM processed_revisions WHERE status='completed'")}


def reconcile_processed_revisions(root: Path) -> dict[str, int]:
    """Recover missed completions as ineligible backfill, without any dispatch."""
    from notifications import record_pipeline_completed_event

    result = {"scanned": 0, "registered": 0, "skipped": 0}
    for path in sorted((root / "jobs").glob("*/manifest.json")):
        result["scanned"] += 1
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            revision, _ = completion_snapshot(root, path.parent, manifest)
        except (OSError, ValueError, TypeError, AttributeError):
            result["skipped"] += 1
            continue
        conn = open_readonly(root)
        with closing(conn) if conn is not None else nullcontext():
            exists = conn is not None and conn.execute(
                "SELECT 1 FROM processed_revisions WHERE job=? AND analysis_revision=?",
                (path.parent.name, revision)).fetchone()
        if exists:
            continue
        event = record_pipeline_completed_event(root, path.parent, manifest, backfill=True)
        result["registered" if event else "skipped"] += 1
    return result
