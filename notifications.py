"""Notification and digest coordination service with operations.db persistence.

Implements N0-N8 requirements:
- Atomic lease claiming and state machine: pending -> sending -> accepted | retry_wait | unknown | dead.
- Delivery attempt auditing.
- Preview and Send pipelines with frozen drafts and payload hash guards.
- Dry-run / local-only safety fences.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from digests import build_digest_payload, collect_digest_candidates, compute_digest_window
from email_providers import BaseProvider, DeliveryResult, get_configured_provider
from email_templates import render_digest_email, render_event_email


def get_operations_db(root: Path) -> sqlite3.Connection:
    """Connect to operations.db and ensure schema migrations are applied."""
    db_path = root / "operations.db"
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    _init_operations_schema(conn)
    return conn


def _init_operations_schema(conn: sqlite3.Connection) -> None:
    """Idempotently initialize tables for notifications and digest runs."""
    with conn:
        conn.executescript(
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
            );

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
            );

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
            );

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
            );

            CREATE TABLE IF NOT EXISTS digest_items (
                digest_id TEXT NOT NULL,
                job TEXT NOT NULL,
                analysis_revision TEXT,
                rank INTEGER NOT NULL,
                reason TEXT,
                disposition TEXT NOT NULL,
                PRIMARY KEY(digest_id, job),
                FOREIGN KEY(digest_id) REFERENCES digest_runs(digest_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries ON notification_deliveries(event_id, channel, recipient_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_runs ON digest_runs(schedule_id, type, window_start, window_end, recipient_id, revision);
            CREATE INDEX IF NOT EXISTS ix_deliveries_status ON notification_deliveries(status, next_attempt_at);
            """
        )


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    library_id: str | None,
    job: str | None,
    analysis_revision: str | None,
    payload: dict[str, Any],
    dedup_key: str,
) -> str:
    """Record an outbox event idempotently. Returns event_id."""
    cur = conn.cursor()
    cur.execute("SELECT event_id FROM notification_events WHERE dedup_key = ?", (dedup_key,))
    existing = cur.fetchone()
    if existing:
        return str(existing["event_id"])

    event_id = str(uuid.uuid4())
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with conn:
        conn.execute(
            """
            INSERT INTO notification_events (
                event_id, event_type, library_id, job, analysis_revision, payload_json, created_at, dedup_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, event_type, library_id, job, analysis_revision, payload_json, _utc_now_iso(), dedup_key),
        )
    return event_id


def enqueue_delivery(
    conn: sqlite3.Connection,
    event_id: str,
    channel: str,
    recipient_id: str,
    provider: str,
    payload_hash: str,
    idempotency_key: str | None = None,
) -> str:
    """Enqueue a delivery attempt for an event. Returns delivery_id."""
    cur = conn.cursor()
    cur.execute(
        "SELECT delivery_id FROM notification_deliveries WHERE event_id = ? AND channel = ? AND recipient_id = ?",
        (event_id, channel, recipient_id),
    )
    row = cur.fetchone()
    if row:
        return str(row["delivery_id"])

    delivery_id = str(uuid.uuid4())
    idemp_key = idempotency_key or f"vtp_{delivery_id}"
    with conn:
        conn.execute(
            """
            INSERT INTO notification_deliveries (
                delivery_id, event_id, channel, recipient_id, provider, status,
                payload_hash, provider_idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (delivery_id, event_id, channel, recipient_id, provider, payload_hash, idemp_key, _utc_now_iso()),
        )
    return delivery_id


def claim_pending_deliveries(
    conn: sqlite3.Connection,
    worker_id: str,
    lease_seconds: int = 60,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Atomically claim pending or retry_wait deliveries within a short transaction."""
    now_iso = _utc_now_iso()
    lease_expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=lease_seconds)).isoformat()
    lease_token = str(uuid.uuid4())

    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.delivery_id, d.event_id, d.channel, d.recipient_id, d.provider,
                   d.payload_hash, d.provider_idempotency_key, d.attempt_count,
                   e.event_type, e.payload_json
            FROM notification_deliveries d
            JOIN notification_events e ON d.event_id = e.event_id
            WHERE d.status IN ('pending', 'retry_wait')
              AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)
              AND (d.lease_expires_at IS NULL OR d.lease_expires_at <= ?)
            LIMIT ?
            """,
            (now_iso, now_iso, limit),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        ids = [row["delivery_id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE notification_deliveries
            SET status = 'sending',
                lease_owner = ?,
                lease_token = ?,
                lease_expires_at = ?,
                submitted_at = ?
            WHERE delivery_id IN ({placeholders})
            """,
            [worker_id, lease_token, lease_expires, now_iso, *ids],
        )

    # Return refreshed rows with lease_token attached
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT d.*, e.event_type, e.payload_json
        FROM notification_deliveries d
        JOIN notification_events e ON d.event_id = e.event_id
        WHERE d.delivery_id IN ({placeholders}) AND d.lease_token = ?
        """,
        [*ids, lease_token],
    )
    return cur.fetchall()


def update_delivery_status(
    conn: sqlite3.Connection,
    delivery_id: str,
    lease_token: str,
    result: DeliveryResult,
    attempt_no: int,
    started_at: str,
) -> None:
    """Record attempt outcome and transition status machine."""
    now_iso = _utc_now_iso()
    attempt_id = str(uuid.uuid4())

    with conn:
        # Record attempt
        conn.execute(
            """
            INSERT INTO delivery_attempts (
                attempt_id, delivery_id, attempt_no, request_id, started_at, finished_at, outcome, safe_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, delivery_id, attempt_no, result.provider_message_id, started_at, now_iso, result.status, result.safe_error),
        )

        if result.status == "accepted":
            conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'accepted',
                    provider_message_id = ?,
                    accepted_at = ?,
                    attempt_count = attempt_count + 1,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    safe_error = NULL
                WHERE delivery_id = ? AND lease_token = ?
                """,
                (result.provider_message_id, now_iso, delivery_id, lease_token),
            )
        elif result.status == "retryable_not_accepted":
            wait_sec = result.retry_after_seconds if result.retry_after_seconds is not None else 60
            next_attempt = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=wait_sec)).isoformat()
            conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'retry_wait',
                    next_attempt_at = ?,
                    attempt_count = attempt_count + 1,
                    safe_error = ?,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE delivery_id = ? AND lease_token = ?
                """,
                (next_attempt, result.safe_error, delivery_id, lease_token),
            )
        elif result.status == "unknown":
            conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'unknown',
                    attempt_count = attempt_count + 1,
                    safe_error = ?,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE delivery_id = ? AND lease_token = ?
                """,
                (result.safe_error, delivery_id, lease_token),
            )
        else:  # permanent_failure
            conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'dead',
                    attempt_count = attempt_count + 1,
                    safe_error = ?,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE delivery_id = ? AND lease_token = ?
                """,
                (result.safe_error, delivery_id, lease_token),
            )


# ── CLI Actions ─────────────────────────────────────────────────────────────


def run_digest_preview(
    root: Path,
    digest_type: str = "daily",
    date_str: str | None = None,
) -> Path:
    """Generate a local markdown preview draft without sending any network request."""
    tz_name = os.getenv("DIGEST_TIMEZONE", "Asia/Taipei")
    win_start, win_end, win_label = compute_digest_window(digest_type, date_str, tz_name=tz_name)
    candidates = collect_digest_candidates(root, win_start, win_end)
    payload = build_digest_payload(candidates, win_start, win_end, win_label, digest_type=digest_type)

    text_body, html_body = render_digest_email(payload)

    out_file = root / f"digest_preview_{digest_type}.md"
    out_file.write_text(
        f"<!-- PREVIEW DRAFT: {win_label} | HASH: {payload['rendered_payload_hash']} -->\n\n"
        f"{text_body}\n\n<!-- HTML BODY -->\n```html\n{html_body}\n```\n",
        encoding="utf-8",
    )
    return out_file


def run_digest_send(
    root: Path,
    digest_type: str = "daily",
    date_str: str | None = None,
    dry_run: bool = False,
    local_only: bool = False,
    provider: BaseProvider | None = None,
) -> dict[str, Any]:
    """Execute digest compilation and dispatch with safety checks."""
    if dry_run or local_only:
        # Failsafe: sending is forbidden in dry-run or local-only mode
        preview_path = run_digest_preview(root, digest_type, date_str)
        return {
            "status": "preview_only",
            "reason": "dry-run or local-only safety fence active",
            "preview_path": str(preview_path),
        }

    email_enabled = os.getenv("NOTIFY_EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
    recipient = os.getenv("EMAIL_TO", "").strip() or os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if not email_enabled and not recipient and provider is None:
        return {
            "status": "skipped",
            "reason": "NOTIFY_EMAIL_ENABLED is false and no recipient configured",
        }

    tz_name = os.getenv("DIGEST_TIMEZONE", "Asia/Taipei")
    win_start, win_end, win_label = compute_digest_window(digest_type, date_str, tz_name=tz_name)
    candidates = collect_digest_candidates(root, win_start, win_end)
    payload = build_digest_payload(candidates, win_start, win_end, win_label, digest_type=digest_type)

    if not payload["selected_items"]:
        return {
            "status": "skipped",
            "reason": "Empty candidate list in time window; no email dispatched",
            "window_label": win_label,
        }

    active_provider = provider or get_configured_provider()
    text_body, html_body = render_digest_email(payload)
    subject = f"【影片知識庫】{'每日' if digest_type == 'daily' else '每週'}精選摘要（{win_label}）"

    conn = get_operations_db(root)
    event_key = f"digest:{digest_type}:{win_start.isoformat()}:{win_end.isoformat()}:{recipient}"
    event_id = record_event(
        conn,
        event_type=f"digest.{digest_type}",
        library_id=None,
        job=None,
        analysis_revision=None,
        payload=payload,
        dedup_key=event_key,
    )

    delivery_id = enqueue_delivery(
        conn,
        event_id=event_id,
        channel="email",
        recipient_id=recipient or "default",
        provider=active_provider.__class__.__name__,
        payload_hash=payload["rendered_payload_hash"],
        idempotency_key=f"digest_{payload['rendered_payload_hash'][:32]}",
    )

    # Claim and send
    worker_id = f"worker_{os.getpid()}"
    claimed = claim_pending_deliveries(conn, worker_id=worker_id, limit=1)
    if not claimed:
        return {"status": "already_claimed_or_sent", "delivery_id": delivery_id}

    target = claimed[0]
    started_at = _utc_now_iso()
    result = active_provider.send(
        recipient=target["recipient_id"],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        idempotency_key=target["provider_idempotency_key"],
    )

    update_delivery_status(
        conn,
        delivery_id=target["delivery_id"],
        lease_token=target["lease_token"],
        result=result,
        attempt_no=int(target["attempt_count"]) + 1,
        started_at=started_at,
    )

    # Record digest run and items
    digest_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO digest_runs (
                digest_id, schedule_id, type, window_start, window_end, timezone, recipient_id,
                revision, selection_snapshot, rendered_payload_hash, status,
                candidate_count, selected_count, deferred_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest_id,
                f"default_{digest_type}",
                digest_type,
                win_start.isoformat(),
                win_end.isoformat(),
                tz_name,
                recipient or "default",
                json.dumps(payload["selected_items"], ensure_ascii=False),
                payload["rendered_payload_hash"],
                result.status,
                payload["candidate_count"],
                payload["selected_count"],
                payload["deferred_count"],
                _utc_now_iso(),
            ),
        )
        for rank, item in enumerate(payload["selected_items"], 1):
            conn.execute(
                """
                INSERT OR REPLACE INTO digest_items (
                    digest_id, job, analysis_revision, rank, reason, disposition
                ) VALUES (?, ?, ?, ?, 'selected_by_time', 'delivered' if ? == 'accepted' else 'pending')
                """,
                (digest_id, item["job"], item.get("analysis_revision", "1"), rank, result.status),
            )

    return {
        "status": result.status,
        "delivery_id": delivery_id,
        "provider_message_id": result.provider_message_id,
        "selected_count": payload["selected_count"],
        "deferred_count": payload["deferred_count"],
        "error": result.safe_error,
    }


def run_notify_test(root: Path, provider: BaseProvider | None = None) -> dict[str, Any]:
    """Dispatch an end-to-end connectivity test email / webhook."""
    active_provider = provider or get_configured_provider()
    recipient = os.getenv("EMAIL_TO", "").strip() or os.getenv("NOTIFY_WEBHOOK_URL", "").strip() or "test@example.com"
    subject = "【系統測試】Video Timeline Pipeline 通知測試信"
    text = f"這是一封來自 Video Timeline Pipeline 的測試通知信。\n發送時間：{_utc_now_iso()}\nProvider: {active_provider.__class__.__name__}"
    html = f"""<div style="font-family:sans-serif;padding:20px;">
        <h2>通知測試成功</h2>
        <p>Video Timeline Pipeline 通知管線運作正常。</p>
        <p><strong>時間：</strong>{_utc_now_iso()}</p>
        <p><strong>Provider：</strong>{active_provider.__class__.__name__}</p>
    </div>"""

    conn = get_operations_db(root)
    event_key = f"test:{_utc_now_iso()}"
    event_id = record_event(
        conn,
        event_type="test.notification",
        library_id=None,
        job=None,
        analysis_revision=None,
        payload={"subject": subject, "text": text},
        dedup_key=event_key,
    )

    delivery_id = enqueue_delivery(
        conn,
        event_id=event_id,
        channel="test",
        recipient_id=recipient,
        provider=active_provider.__class__.__name__,
        payload_hash=hashlib.sha256(text.encode()).hexdigest(),
    )

    claimed = claim_pending_deliveries(conn, worker_id=f"test_{os.getpid()}", limit=1)
    if not claimed:
        return {"status": "claim_failed", "delivery_id": delivery_id}

    target = claimed[0]
    started = _utc_now_iso()
    result = active_provider.send(
        recipient=target["recipient_id"],
        subject=subject,
        text_body=text,
        html_body=html,
        idempotency_key=target["provider_idempotency_key"],
    )

    update_delivery_status(
        conn,
        delivery_id=target["delivery_id"],
        lease_token=target["lease_token"],
        result=result,
        attempt_no=1,
        started_at=started,
    )

    return {
        "status": result.status,
        "delivery_id": delivery_id,
        "provider_message_id": result.provider_message_id,
        "safe_error": result.safe_error,
    }


def run_notification_retry(root: Path, provider: BaseProvider | None = None) -> dict[str, int]:
    """Retry deliveries that are in retry_wait status."""
    conn = get_operations_db(root)
    active_provider = provider or get_configured_provider()
    worker_id = f"retry_worker_{os.getpid()}"

    claimed = claim_pending_deliveries(conn, worker_id=worker_id, limit=20)
    retried_count = 0
    accepted_count = 0

    for item in claimed:
        retried_count += 1
        started = _utc_now_iso()
        # Parse payload
        try:
            payload = json.loads(item["payload_json"])
            subject = f"【重試通知】{item['event_type']}"
            text_body = json.dumps(payload, ensure_ascii=False, indent=2)
            html_body = None
        except Exception:
            subject = f"【重試通知】{item['event_type']}"
            text_body = "無法解碼 payload"
            html_body = None

        result = active_provider.send(
            recipient=item["recipient_id"],
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            idempotency_key=item["provider_idempotency_key"],
        )

        update_delivery_status(
            conn,
            delivery_id=item["delivery_id"],
            lease_token=item["lease_token"],
            result=result,
            attempt_no=int(item["attempt_count"]) + 1,
            started_at=started,
        )
        if result.status == "accepted":
            accepted_count += 1

    return {"claimed": len(claimed), "retried": retried_count, "accepted": accepted_count}


def record_pipeline_completed_event(root: Path, job_dir: Path, manifest: dict[str, Any]) -> str | None:
    """Invoked when a video pipeline run completes: records event and triggers delivery if configured."""
    try:
        conn = get_operations_db(root)
        job = job_dir.name
        source = manifest.get("source", {})
        title = source.get("title", job)
        url = source.get("url", "")
        revision = manifest.get("analysis_revision", "1")
        dedup_key = f"video.completed:{job}:{revision}"

        event_payload = {
            "job": job,
            "title": title,
            "url": url,
            "analysis_revision": revision,
            "created_at": _utc_now_iso(),
        }

        event_id = record_event(
            conn,
            event_type="video.completed",
            library_id=manifest.get("library_id"),
            job=job,
            analysis_revision=revision,
            payload=event_payload,
            dedup_key=dedup_key,
        )

        # If Webhook or high priority notification is active, enqueue delivery
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("RSS_NOTIFY_WEBHOOK")
        if webhook_url:
            delivery_id = enqueue_delivery(
                conn,
                event_id=event_id,
                channel="webhook",
                recipient_id=webhook_url,
                provider="WebhookProvider",
                payload_hash=hashlib.sha256(dedup_key.encode()).hexdigest(),
            )
            # Dispatch immediately
            claimed = claim_pending_deliveries(conn, worker_id=f"pipeline_{os.getpid()}", limit=1)
            if claimed:
                target = claimed[0]
                text, html_msg = render_event_email("video.completed", event_payload)
                provider = get_configured_provider("webhook")
                started = _utc_now_iso()
                result = provider.send(
                    recipient=target["recipient_id"],
                    subject=f"【影片處理完成】{title}",
                    text_body=text,
                    html_body=html_msg,
                    idempotency_key=target["provider_idempotency_key"],
                )
                update_delivery_status(
                    conn,
                    delivery_id=target["delivery_id"],
                    lease_token=target["lease_token"],
                    result=result,
                    attempt_no=1,
                    started_at=started,
                )

        return event_id
    except Exception as exc:
        # Failsafe: notification failures MUST NEVER ruin pipeline artifacts or throw
        print(f"! 通知佇列寫入異常（不影響影片產物）：{exc}")
        return None
