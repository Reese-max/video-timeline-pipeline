"""Durable task queue for the web dashboard (issue #6, W7).

FastAPI-style in-memory background tasks are not a reliable queue: this
module persists every side-effecting request (semantic search, Ask, RSS
item retry, digest send) as a row in ``operations.db``'s
``operation_tasks`` table. Workers claim tasks inside short transactions
with a lease + fencing token, heartbeat while running, and recover
expired leases on restart — tasks that already reached a provider are
marked ``unknown`` instead of blindly re-executed.

Reads in this module never create schema; ``init_task_schema`` runs only
on write paths (enqueue / worker / explicit init).
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "unknown",
        "cancel_requested",
        "cancelled",
    }
)

# Allowlisted task types — the API must never accept arbitrary command
# strings, file paths, or provider URLs (W7).
TASK_TYPES = frozenset({"semantic_search", "ask", "rss_item_retry", "digest_send"})

_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_DEFAULT_LEASE_SECONDS = 60.0
_RETRY_BASE_SECONDS = 30.0
_MAX_RESULT_CHARS = 200_000
_MAX_ERROR_CHARS = 500

Handler = Callable[[Path, dict[str, Any]], dict[str, Any]]


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _safe_error(exc: BaseException | str) -> str:
    """Trim provider/OS detail out of persisted errors; never keep secrets."""
    text = str(exc) if not isinstance(exc, str) else exc
    text = " ".join(text.split())
    return text[:_MAX_ERROR_CHARS]


def init_task_schema(conn: sqlite3.Connection) -> None:
    """Create operation_tasks (idempotent). Call only on write paths."""
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS operation_tasks (
                task_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                job TEXT,
                revision TEXT,
                safe_payload TEXT NOT NULL DEFAULT '{}',
                dedup_key TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued',
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                next_attempt_at TEXT,
                checkpoint TEXT,
                remote_operation_id TEXT,
                result_json TEXT,
                safe_error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_operation_tasks_status
                ON operation_tasks(status, next_attempt_at);
            """
        )


def _connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(root / "operations.db", timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _operations_db_present(root: Path) -> bool:
    return (root / "operations.db").is_file()


def _table_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='operation_tasks'"
    ).fetchone()
    return row is not None


def tasks_table_ready(root: Path) -> bool:
    if not _operations_db_present(root):
        return False
    with closing(_connect(root)) as conn:
        return _table_present(conn)


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    for key in ("safe_payload", "checkpoint", "result_json"):
        raw = task.pop(key)
        name = {"safe_payload": "payload", "checkpoint": "checkpoint", "result_json": "result"}[key]
        try:
            task[name] = json.loads(raw) if raw else ({} if key != "result_json" else None)
        except (json.JSONDecodeError, TypeError):
            task[name] = {} if key != "result_json" else None
    return task


def enqueue_task(
    root: Path,
    task_type: str,
    *,
    job: str | None = None,
    revision: str | None = None,
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Insert a queued task; same dedup_key returns the existing row."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"task type not allowed: {task_type}")
    max_attempts = max(1, min(int(max_attempts), 10))
    safe_payload = payload or {}
    task_id = str(uuid.uuid4())
    now = _iso(_utc_now())
    with closing(_connect(root)) as conn:
        init_task_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO operation_tasks (
                    task_id, type, job, revision, safe_payload, dedup_key,
                    status, attempts, max_attempts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                """,
                (
                    task_id,
                    task_type,
                    job,
                    revision,
                    json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                    dedup_key,
                    max_attempts,
                    now,
                ),
            )
        row = conn.execute(
            "SELECT * FROM operation_tasks WHERE dedup_key = ?" if dedup_key else "SELECT * FROM operation_tasks WHERE task_id = ?",
            (dedup_key or task_id,),
        ).fetchone()
    task = _row_to_task(row)
    task["created"] = bool(row["task_id"] == task_id)
    return task


def get_task(root: Path, task_id: str) -> dict[str, Any] | None:
    """Read a task. Polling never mutates state (W7)."""
    if not _operations_db_present(root):
        return None
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            return None
        row = conn.execute("SELECT * FROM operation_tasks WHERE task_id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def list_tasks(root: Path, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if not _operations_db_present(root):
        return []
    limit = max(1, min(int(limit), 200))
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            return []
        if status:
            rows = conn.execute(
                "SELECT * FROM operation_tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM operation_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_task(row) for row in rows]


def claim_next_task(
    root: Path,
    worker_id: str,
    *,
    lease_seconds: float = _DEFAULT_LEASE_SECONDS,
    allowed_types: set[str] | None = None,
    per_type_running_limit: int = 2,
) -> dict[str, Any] | None:
    """Atomically claim the oldest due queued task inside BEGIN IMMEDIATE.

    Per-type concurrency is capped so the web worker does not race the
    RSS single-writer or hammer one provider (W7).
    """
    now = _utc_now()
    now_s = _iso(now)
    lease_until = _iso(now + datetime.timedelta(seconds=lease_seconds))
    types = sorted(allowed_types) if allowed_types else sorted(TASK_TYPES)
    placeholders = ",".join("?" for _ in types)
    with closing(_connect(root)) as conn:
        init_task_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"""
                SELECT task_id, type FROM operation_tasks
                WHERE status = 'queued'
                  AND type IN ({placeholders})
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at, task_id
                LIMIT 8
                """,
                (*types, now_s),
            ).fetchall()
            for row in rows:
                running = conn.execute(
                    "SELECT COUNT(*) FROM operation_tasks WHERE type = ? AND status = 'running'",
                    (row["type"],),
                ).fetchone()[0]
                if running >= per_type_running_limit:
                    continue
                token = str(uuid.uuid4())
                updated = conn.execute(
                    """
                    UPDATE operation_tasks
                    SET status = 'running', lease_owner = ?, lease_token = ?,
                        lease_expires_at = ?, heartbeat_at = ?, attempts = attempts + 1,
                        started_at = COALESCE(started_at, ?)
                    WHERE task_id = ? AND status = 'queued'
                    """,
                    (worker_id, token, lease_until, now_s, now_s, row["task_id"]),
                )
                if updated.rowcount != 1:
                    continue
                conn.commit()
                return get_task(root, row["task_id"])
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise


def _fenced_update(
    root: Path,
    task_id: str,
    lease_token: str,
    set_clause: str,
    params: tuple[Any, ...],
) -> bool:
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            return False
        with conn:
            cur = conn.execute(
                f"UPDATE operation_tasks SET {set_clause} WHERE task_id = ? AND lease_token = ? AND status IN ('running','cancel_requested')",
                (*params, task_id, lease_token),
            )
        return cur.rowcount == 1


def heartbeat(
    root: Path,
    task_id: str,
    lease_token: str,
    *,
    checkpoint: dict[str, Any] | None = None,
    lease_seconds: float = _DEFAULT_LEASE_SECONDS,
) -> bool:
    now = _utc_now()
    clause = "heartbeat_at = ?, lease_expires_at = ?"
    params: list[Any] = [_iso(now), _iso(now + datetime.timedelta(seconds=lease_seconds))]
    if checkpoint is not None:
        clause += ", checkpoint = ?"
        params.append(json.dumps(checkpoint, ensure_ascii=False))
    return _fenced_update(root, task_id, lease_token, clause, tuple(params))


def complete_task(
    root: Path,
    task_id: str,
    lease_token: str,
    *,
    result: dict[str, Any] | None = None,
    remote_operation_id: str | None = None,
) -> bool:
    now = _iso(_utc_now())
    result_json = json.dumps(result, ensure_ascii=False)[:_MAX_RESULT_CHARS] if result is not None else None
    return _fenced_update(
        root,
        task_id,
        lease_token,
        "status = 'succeeded', result_json = ?, remote_operation_id = COALESCE(?, remote_operation_id),"
        " finished_at = ?, lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL",
        (result_json, remote_operation_id, now),
    )


def fail_task(
    root: Path,
    task_id: str,
    lease_token: str,
    safe_error: BaseException | str,
    *,
    retryable: bool = True,
) -> bool:
    """Fail a claimed task. Retryable errors requeue with exponential backoff."""
    now = _utc_now()
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            return False
        row = conn.execute(
            "SELECT attempts, max_attempts, status FROM operation_tasks WHERE task_id = ? AND lease_token = ?",
            (task_id, lease_token),
        ).fetchone()
        if row is None or row["status"] not in ("running", "cancel_requested"):
            return False
        error = _safe_error(safe_error)
        attempts_left = int(row["attempts"]) < int(row["max_attempts"])
        if retryable and attempts_left:
            backoff = min(_RETRY_BASE_SECONDS * (2 ** max(0, int(row["attempts"]) - 1)), 3600.0)
            next_at = _iso(now + datetime.timedelta(seconds=backoff))
            with conn:
                conn.execute(
                    """
                    UPDATE operation_tasks
                    SET status = 'queued', safe_error = ?, next_attempt_at = ?,
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                    WHERE task_id = ?
                    """,
                    (error, next_at, task_id),
                )
        else:
            with conn:
                conn.execute(
                    """
                    UPDATE operation_tasks
                    SET status = 'failed', safe_error = ?, finished_at = ?,
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                    WHERE task_id = ?
                    """,
                    (error, _iso(now), task_id),
                )
        return True


def request_cancel(root: Path, task_id: str) -> str:
    """Cancel queued work or request cancellation of a running task.

    Cancel stops future steps only — a request already sent to a provider
    may still complete or be billed; callers must not promise refunds (W7).
    """
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            raise ValueError("task store unavailable")
        row = conn.execute("SELECT status FROM operation_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        status = row["status"]
        if status in _TERMINAL_STATUSES:
            raise ValueError(f"task already terminal: {status}")
        new_status = "cancelled" if status == "queued" else "cancel_requested"
        with conn:
            conn.execute(
                "UPDATE operation_tasks SET status = ?, finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END WHERE task_id = ?",
                (new_status, new_status, _iso(_utc_now()), task_id),
            )
        return new_status


def recover_expired_leases(root: Path, *, now: datetime.datetime | None = None) -> list[str]:
    """Requeue tasks whose lease expired.

    A task that never reached a provider is safe to requeue; one holding a
    remote_operation_id becomes ``unknown`` — its remote state must be
    reconciled before any re-run (W7 crash recovery).
    """
    now_s = _iso(now or _utc_now())
    moved: list[str] = []
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            return moved
        with conn:
            rows = conn.execute(
                """
                SELECT task_id, remote_operation_id, attempts, max_attempts FROM operation_tasks
                WHERE status IN ('running', 'cancel_requested') AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (now_s,),
            ).fetchall()
            for row in rows:
                if row["remote_operation_id"]:
                    conn.execute(
                        "UPDATE operation_tasks SET status = 'unknown', safe_error = ?, lease_owner = NULL, lease_token = NULL WHERE task_id = ?",
                        ("lease expired after remote submission; reconcile remote state", row["task_id"]),
                    )
                elif int(row["attempts"]) >= int(row["max_attempts"]):
                    conn.execute(
                        "UPDATE operation_tasks SET status = 'failed', safe_error = ?, finished_at = ?, lease_owner = NULL, lease_token = NULL WHERE task_id = ?",
                        ("lease expired without checkpoint", now_s, row["task_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE operation_tasks SET status = 'queued', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, safe_error = ? WHERE task_id = ?",
                        ("lease expired; requeued", row["task_id"]),
                    )
                moved.append(row["task_id"])
    return moved


def _execute(root: Path, task: dict[str, Any], handler: Handler, worker_id: str, heartbeat_seconds: float) -> None:
    """Run one claimed task's handler; route the outcome through fencing."""
    task_id, token = task["task_id"], task["lease_token"]
    if task["status"] == "cancel_requested":
        _fenced_update(
            root, task_id, token,
            "status = 'cancelled', finished_at = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL",
            (_iso(_utc_now()),),
        )
        return
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(heartbeat_seconds):
            try:
                heartbeat(root, task_id, token)
            except sqlite3.Error:
                pass

    beater = threading.Thread(target=_beat, daemon=True)
    beater.start()
    try:
        result = handler(root, task["payload"])
    except Exception as exc:  # handler boundary: safe_error only
        stop.set()
        fail_task(root, task_id, token, exc, retryable=True)
        return
    finally:
        stop.set()
    refreshed = get_task(root, task_id)
    if refreshed is not None and refreshed["status"] == "cancel_requested":
        _fenced_update(
            root, task_id, token,
            "status = 'cancelled', finished_at = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL",
            (_iso(_utc_now()),),
        )
        return
    complete_task(root, task_id, token, result=result)


def _fail_disallowed_types(root: Path) -> int:
    """Rows with types outside the allowlist can never be executed — mark
    them failed so they don't sit invisibly in the queue (W7 allowlist)."""
    placeholders = ",".join("?" for _ in sorted(TASK_TYPES))
    with closing(_connect(root)) as conn:
        if not _table_present(conn):
            return 0
        with conn:
            cur = conn.execute(
                f"""
                UPDATE operation_tasks
                SET status = 'failed', safe_error = 'task type not in allowlist; no handler registered',
                    finished_at = ?
                WHERE status = 'queued' AND type NOT IN ({placeholders})
                """,
                (_iso(_utc_now()), *sorted(TASK_TYPES)),
            )
        return cur.rowcount


def run_worker(
    root: Path,
    *,
    worker_id: str | None = None,
    handlers: dict[str, Handler] | None = None,
    once: bool = False,
    poll_seconds: float = 1.0,
    lease_seconds: float = _DEFAULT_LEASE_SECONDS,
) -> int:
    """Worker loop: recover expired leases, claim, execute allowlisted handlers.

    ``once=True`` drains currently-due work and returns the processed count —
    used by tests and by ``pipeline.py --serve-worker-once``.
    """
    worker = worker_id or f"web-worker-{os.getpid()}"
    registry = handlers if handlers is not None else TASK_HANDLERS
    processed = 0
    while True:
        recover_expired_leases(root)
        processed += _fail_disallowed_types(root)
        task = claim_next_task(root, worker, lease_seconds=lease_seconds, allowed_types=set(registry))
        if task is None:
            if once:
                return processed
            time.sleep(poll_seconds)
            continue
        handler = registry.get(task["type"])
        if handler is None:
            fail_task(root, task["task_id"], task["lease_token"], f"no handler registered for {task['type']}", retryable=False)
            processed += 1
            continue
        _execute(root, task, handler, worker, max(5.0, lease_seconds / 3))
        processed += 1
        if once:
            # keep draining until no due work
            continue


# ---------------------------------------------------------------------------
# Built-in handlers — each calls the existing service layer; nothing here
# re-implements provider transports (issue #6 W3/W4/W6).
# ---------------------------------------------------------------------------


def _handle_semantic_search(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import pipeline  # lazy: heavy module stays out of the web import graph

    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("query required")
    backend = str(payload.get("backend") or "openrouter")
    model = str(payload.get("model") or os.getenv("EMBED_MODEL") or "")
    if not model:
        raise ValueError("embedding model not configured (EMBED_MODEL)")
    limit = max(1, min(int(payload.get("limit") or 20), 50))
    api_key = os.getenv("OPENROUTER_API_KEY", "") if backend == "openrouter" else ""
    if backend == "openrouter" and not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    import cost_tracker

    reservation_id = payload.get("reservation_id")
    started = time.monotonic()
    try:
        hits = pipeline.semantic_search(root, query, api_key, model, limit, backend)
    except Exception as exc:
        cost_tracker.record_usage(
            root,
            provider="openrouter" if backend == "openrouter" else "local",
            operation="semantic_search",
            model=model,
            latency_ms=(time.monotonic() - started) * 1000,
            status="failed",
            safe_error=_safe_error(exc),
            reservation_id=reservation_id,
        )
        raise
    cost_tracker.record_usage(
        root,
        provider="openrouter" if backend == "openrouter" else "local",
        operation="semantic_search",
        model=model,
        request_units=len(query),
        latency_ms=(time.monotonic() - started) * 1000,
        status="succeeded",
        reservation_id=reservation_id,
    )
    return {"mode": "semantic", "backend": backend, "model": model, "hits": hits[:limit]}


def _handle_ask(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import pipeline

    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("question required")
    limit = max(1, min(int(payload.get("limit") or 12), 30))
    api_key = os.getenv("MINIMAX_SUBSCRIPTION_KEY", "")
    model = os.getenv("MINIMAX_MODEL", "")
    if not api_key or not model:
        raise RuntimeError("MINIMAX_SUBSCRIPTION_KEY/MINIMAX_MODEL not configured")
    embed_key = os.getenv("OPENROUTER_API_KEY", "")
    embed_model = os.getenv("EMBED_MODEL", "")
    embed_backend = "openrouter" if embed_key and embed_model else ""

    import cost_tracker

    reservation_id = payload.get("reservation_id")
    started = time.monotonic()
    try:
        result = pipeline.answer_question(
            root, question, api_key, model, limit, embed_key, embed_model, embed_backend
        )
    except Exception as exc:
        cost_tracker.record_usage(
            root,
            provider="minimax",
            operation="ask",
            model=model,
            latency_ms=(time.monotonic() - started) * 1000,
            status="failed",
            safe_error=_safe_error(exc),
            reservation_id=reservation_id,
        )
        raise
    cost_tracker.record_usage(
        root,
        provider="minimax",
        operation="ask",
        model=model,
        request_units=len(question),
        latency_ms=(time.monotonic() - started) * 1000,
        status="succeeded",
        reservation_id=reservation_id,
    )
    answer = {"answer": result.get("answer", ""), "keywords": result.get("keywords", []), "citations": []}
    for hit in (result.get("citations") or [])[:limit]:
        answer["citations"].append(
            {
                "job": hit.get("job"),
                "title": hit.get("title"),
                "timestamp": hit.get("timestamp"),
                "kind": hit.get("kind"),
                "snippet": hit.get("snippet") or hit.get("text", "")[:280],
            }
        )
    return answer


def _handle_rss_item_retry(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import pipeline

    item_id = str(payload.get("item_id") or "").strip()
    if not item_id:
        raise ValueError("item_id required")
    with pipeline.rss_writer_lock(root):
        item = pipeline.requeue_rss_item(root, item_id)
    return {"item_id": item_id, "status": item.get("status"), "subscription_id": item.get("subscription_id")}


def _handle_digest_send(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Send only the exact previewed digest payload — hash-verified, idempotent."""
    import digests
    import email_providers
    import email_templates
    import notifications

    digest_id = str(payload.get("digest_id") or "")
    revision = int(payload.get("revision") or 0)
    expected_hash = str(payload.get("payload_hash") or "")
    digest_type = str(payload.get("digest_type") or "daily")
    date_str = payload.get("date_str")

    with closing(_connect(root)) as conn:
        row = conn.execute(
            "SELECT * FROM digest_runs WHERE digest_id = ?", (digest_id,)
        ).fetchone()
    if row is None:
        raise ValueError("unknown digest_id")
    if int(row["revision"]) != revision or row["rendered_payload_hash"] != expected_hash:
        raise ValueError("stale_preview: digest changed since preview")
    if row["status"] in ("accepted", "delivered"):
        return {"status": "already_sent", "digest_id": digest_id}

    tz_name = os.getenv("DIGEST_TIMEZONE", "Asia/Taipei")
    win_start, win_end, win_label = digests.compute_digest_window(digest_type, date_str, tz_name=tz_name)
    candidates = digests.collect_digest_candidates(root, win_start, win_end)
    rebuilt = digests.build_digest_payload(candidates, win_start, win_end, win_label, digest_type=digest_type)
    snapshot = json.loads(row["selection_snapshot"] or "{}")
    expected_signature = snapshot.get("content_signature")
    if expected_signature is not None:
        import web_services

        if web_services.digest_content_signature(rebuilt) != expected_signature:
            raise ValueError("stale_payload: content changed since preview; re-preview required")

    email_enabled = os.getenv("NOTIFY_EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
    recipient = os.getenv("EMAIL_TO", "").strip() or os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if not email_enabled and not recipient:
        raise RuntimeError("email notifications not configured (NOTIFY_EMAIL_ENABLED/EMAIL_TO)")
    if not rebuilt["selected_items"]:
        raise RuntimeError("empty candidate list; nothing to send")

    provider = email_providers.get_configured_provider()
    text_body, html_body = email_templates.render_digest_email(rebuilt)
    subject = f"【影片知識庫】{'每日' if digest_type == 'daily' else '每週'}精選摘要（{win_label}）"

    with closing(notifications.get_operations_db(root)) as conn:
        event_key = f"digest:{digest_type}:{win_start.isoformat()}:{win_end.isoformat()}:{recipient}"
        event_id = notifications.record_event(
            conn,
            event_type=f"digest.{digest_type}",
            library_id=None,
            job=None,
            analysis_revision=None,
            payload=rebuilt,
            dedup_key=event_key,
        )
        delivery_id = notifications.enqueue_delivery(
            conn,
            event_id=event_id,
            channel="email",
            recipient_id=recipient or "default",
            provider=provider.__class__.__name__,
            payload_hash=rebuilt["rendered_payload_hash"],
            idempotency_key=f"digest_{rebuilt['rendered_payload_hash'][:32]}",
        )
        existing = conn.execute(
            "SELECT status FROM notification_deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if existing and existing["status"] in ("accepted", "delivered"):
            with conn:
                conn.execute(
                    "UPDATE digest_runs SET status = ? WHERE digest_id = ?", (existing["status"], digest_id)
                )
            return {"status": "already_sent", "digest_id": digest_id, "delivery_id": delivery_id}
        claimed = notifications.claim_pending_deliveries(conn, worker_id=f"web_task_{os.getpid()}", limit=25)
        target = next((r for r in claimed if r["delivery_id"] == delivery_id), None)
        if target is None:
            # someone else holds the lease — surface as unknown rather than resend
            conn.execute(
                "UPDATE digest_runs SET status = 'unknown' WHERE digest_id = ?", (digest_id,)
            )
            conn.commit()
            raise RuntimeError("delivery claimed by another worker; state unknown")
        # release collaterally claimed deliveries back to pending
        stray = [r["delivery_id"] for r in claimed if r["delivery_id"] != delivery_id]
        if stray:
            with conn:
                conn.execute(
                    f"""
                    UPDATE notification_deliveries
                    SET status = 'pending', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                    WHERE lease_token = ? AND delivery_id IN ({','.join('?' for _ in stray)})
                    """,
                    (target["lease_token"], *stray),
                )

        started_at = _iso(_utc_now())
        result = provider.send(
            recipient=target["recipient_id"],
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            idempotency_key=target["provider_idempotency_key"],
        )
        notifications.update_delivery_status(
            conn,
            delivery_id=target["delivery_id"],
            lease_token=target["lease_token"],
            result=result,
            attempt_no=int(target["attempt_count"]) + 1,
            started_at=started_at,
        )
        with conn:
            conn.execute(
                "UPDATE digest_runs SET status = ? WHERE digest_id = ?",
                (result.status, digest_id),
            )
    return {
        "status": result.status,
        "digest_id": digest_id,
        "delivery_id": delivery_id,
        "provider_message_id": result.provider_message_id,
    }


TASK_HANDLERS: dict[str, Handler] = {
    "semantic_search": _handle_semantic_search,
    "ask": _handle_ask,
    "rss_item_retry": _handle_rss_item_retry,
    "digest_send": _handle_digest_send,
}
