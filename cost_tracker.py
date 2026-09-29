"""API cost tracking, token accounting, and budget guard service (Issue #4).

Stores financial and usage records in operations.db.
Guarantees:
- Integer micro-USD storage ($1 = 1,000,000 micro-USD), prohibiting float drift in balances.
- Atomic budget reservations with settled + outstanding + proposed upper bounds.
- Status separation: HTTP outcome vs billing status (actual | estimated | confirmed_included | unknown).
- Pure local read-only cost reporting without triggering external paid network requests.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notifications import get_operations_db

CostStatus = Literal["actual", "estimated", "confirmed_included", "unknown", "zero_local"]
UsageStatus = Literal["reserved", "sent", "succeeded", "failed", "unknown", "blocked_budget", "cache_hit"]
ErrorTaxonomy = Literal[
    "auth",
    "permission",
    "invalid_request",
    "rate_limit",
    "timeout",
    "network",
    "provider_5xx",
    "quota_exhausted",
    "budget_blocked",
    "content_rejected",
    "unknown",
]


def classify_error(
    error: Exception | str | None,
    status_code: int | None = None,
    response_body: str | dict | None = None,
) -> ErrorTaxonomy:
    """Classify external API or transport failure according to C4 taxonomy."""
    if error is None and status_code is None:
        return "unknown"

    err_str = str(error or "").lower()
    body_str = json.dumps(response_body).lower() if isinstance(response_body, dict) else str(response_body or "").lower()
    combined = f"{err_str} {body_str}"

    if "budget blocked" in combined or "budget_blocked" in combined:
        return "budget_blocked"

    if status_code in (401, 403) or "unauthorized" in combined or "invalid_api_key" in combined or "forbidden" in combined:
        if "permission" in combined or "insufficient_scope" in combined or "access_denied" in combined:
            return "permission"
        return "auth"

    if "quota" in combined or "insufficient_quota" in combined or "credit_exhausted" in combined or "credits exhausted" in combined:
        return "quota_exhausted"

    if status_code == 429 or "rate_limit" in combined or "too many requests" in combined:
        if "quota" in combined or "insufficient_quota" in combined or "credit" in combined:
            return "quota_exhausted"
        return "rate_limit"

    if status_code == 400 or "invalid_request" in combined or "bad request" in combined:
        if "content_filter" in combined or "safety" in combined or "content policy" in combined:
            return "content_rejected"
        return "invalid_request"

    if "timeout" in combined or "timed out" in combined or status_code in (408, 504):
        return "timeout"

    if "connection" in combined or "network" in combined or "dns" in combined or "econnrefused" in combined:
        return "network"

    if status_code is not None and 500 <= status_code < 600:
        return "provider_5xx"

    return "unknown"


def is_retryable(category: ErrorTaxonomy) -> bool:
    """Safe retry predicate under C4 policy."""
    return category in ("rate_limit", "timeout", "network", "provider_5xx")


def _init_cost_schema(conn: sqlite3.Connection) -> None:
    """Idempotently initialize usage and budget tables in operations.db."""
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_costs (
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
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS budget_reservations (
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
            );

            CREATE INDEX IF NOT EXISTS ix_costs_job ON usage_costs(job, provider);
            CREATE INDEX IF NOT EXISTS ix_costs_period ON usage_costs(created_at, provider);
            CREATE INDEX IF NOT EXISTS ix_budget_period ON budget_reservations(period_id, status);
            """
        )


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass(frozen=True)
class _PeriodWindow:
    period_id: str
    timezone: str
    start_utc: datetime.datetime
    end_utc: datetime.datetime


def _current_period_window(
    period_type: str = "daily",
    now: datetime.datetime | None = None,
) -> _PeriodWindow:
    """Return the configured local calendar period and its half-open UTC window."""
    if period_type not in {"daily", "monthly"}:
        raise ValueError(f"Unsupported cost period type: {period_type!r}")

    tz_name = os.getenv("COST_TIMEZONE", "Asia/Taipei")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid COST_TIMEZONE {tz_name!r}; set it to a valid IANA timezone name."
        ) from exc

    current_utc = now or datetime.datetime.now(datetime.timezone.utc)
    if current_utc.tzinfo is None:
        raise ValueError("Cost period calculation requires a timezone-aware datetime.")
    local_now = current_utc.astimezone(tz)

    if period_type == "daily":
        local_date = local_now.date()
        period_id = local_date.isoformat()
        next_date = local_date + datetime.timedelta(days=1)
        start_local = datetime.datetime(
            local_date.year, local_date.month, local_date.day, tzinfo=tz
        )
        end_local = datetime.datetime(
            next_date.year, next_date.month, next_date.day, tzinfo=tz
        )
    else:
        period_id = local_now.strftime("%Y-%m")
        next_year = local_now.year + (1 if local_now.month == 12 else 0)
        next_month = 1 if local_now.month == 12 else local_now.month + 1
        start_local = datetime.datetime(local_now.year, local_now.month, 1, tzinfo=tz)
        end_local = datetime.datetime(next_year, next_month, 1, tzinfo=tz)

    return _PeriodWindow(
        period_id=period_id,
        timezone=tz_name,
        start_utc=start_local.astimezone(datetime.timezone.utc),
        end_utc=end_local.astimezone(datetime.timezone.utc),
    )


def _current_period_id(period_type: str = "daily") -> str:
    """Compatibility helper for callers that only need the local period label."""
    return _current_period_window(period_type).period_id


def estimate_operation_cost_micro_usd(provider: str, model: str | None, prompt_tokens: int, completion_tokens: int) -> int:
    """Estimate operation cost in micro-USD ($1 = 1,000,000 uUSD)."""
    p = provider.lower()
    m = (model or "").lower()

    if p == "groq":
        # whisper-large-v3 is approx $0.000185/sec or ~$0.111/hr
        # Roughly estimated per audio chunk
        return 1000  # $0.001 per chunk default
    if p == "minimax":
        # MiniMax M3 approx $0.0003 per 1k input, $0.0012 per 1k output
        input_cost = int((prompt_tokens / 1000.0) * 300)
        output_cost = int((completion_tokens / 1000.0) * 1200)
        return max(100, input_cost + output_cost)
    if p == "openrouter":
        return int((prompt_tokens / 1000.0) * 200)
    if p in ("apify", "brightdata"):
        return 50000  # $0.05 default estimate per scrape run

    return 0


def check_and_reserve_budget(
    root: Path,
    operation_name: str,
    estimated_micro_usd: int,
    account_scope: str = "default",
) -> tuple[bool, str | None, str]:
    """Check budget limits and atomically reserve funds before external network dispatch.

    Returns: (allowed, reservation_id, reason)
    """
    hard_stop = os.getenv("BUDGET_HARD_STOP", "false").lower() in ("true", "1", "yes")
    daily_budget_usd = float(os.getenv("DAILY_BUDGET_USD", "0"))
    daily_budget_micro = int(daily_budget_usd * 1_000_000)
    window = _current_period_window("daily")
    period_id = window.period_id
    start_utc = window.start_utc.isoformat()
    end_utc = window.end_utc.isoformat()

    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        with conn:
            cur = conn.cursor()
            # Sum settled actual/estimated costs for today
            cur.execute(
                """
                SELECT COALESCE(SUM(estimated_micro_usd), 0)
                FROM usage_costs
                WHERE account_scope = ? AND created_at >= ? AND created_at < ?
                  AND status != 'blocked_budget'
                """,
                (account_scope, start_utc, end_utc),
            )
            settled_micro = int(cur.fetchone()[0])

            # Sum outstanding reservations
            cur.execute(
                """
                SELECT COALESCE(SUM(max_micro_usd), 0)
                FROM budget_reservations
                WHERE account_scope = ? AND period_id = ? AND status = 'reserved'
                """,
                (account_scope, period_id),
            )
            outstanding_micro = int(cur.fetchone()[0])

            total_projected = settled_micro + outstanding_micro + estimated_micro_usd

            if hard_stop and daily_budget_micro > 0 and total_projected > daily_budget_micro:
                reason = (
                    f"Budget blocked: projected ({total_projected / 1_000_000:.3f} USD) "
                    f"exceeds daily limit ({daily_budget_usd:.2f} USD)"
                )
                # Record blocked usage
                conn.execute(
                    """
                    INSERT INTO usage_costs (
                        cost_id, operation, provider, status, cost_status,
                        estimated_micro_usd, started_at, created_at, safe_error
                    ) VALUES (?, ?, 'system', 'blocked_budget', 'zero_local', ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), operation_name, estimated_micro_usd, _utc_now_iso(), _utc_now_iso(), reason),
                )
                try:
                    from notifications import record_event

                    record_event(
                        conn,
                        event_type="budget.blocked",
                        library_id=None,
                        job=None,
                        analysis_revision=None,
                        payload={
                            "reason": reason,
                            "period_id": period_id,
                            "accounting_timezone": window.timezone,
                            "period_start_utc": start_utc,
                            "period_end_utc": end_utc,
                            "account_scope": account_scope,
                        },
                        dedup_key=f"budget_blocked_{window.timezone}_{period_id}_{account_scope}",
                    )
                except Exception:
                    pass
                return False, None, reason

            reservation_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO budget_reservations (
                    reservation_id, operation_id, account_scope, period_id, max_micro_usd,
                    status, lease_token, created_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (reservation_id, operation_name, account_scope, period_id, estimated_micro_usd, str(uuid.uuid4()), _utc_now_iso()),
            )

        return True, reservation_id, "Approved"


def record_usage(
    root: Path,
    provider: str,
    operation: str,
    job: str | None = None,
    library_id: str | None = None,
    analysis_revision: str | None = None,
    model: str | None = None,
    request_units: int = 0,
    response_units: int = 0,
    latency_ms: float = 0.0,
    cache_hit: bool = False,
    status: UsageStatus = "succeeded",
    safe_error: str | None = None,
    provider_request_id: str | None = None,
    reservation_id: str | None = None,
    actual_micro_usd: int | None = None,
) -> str:
    """Record a completed or failed operation attempt and settle reservation."""
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        cost_id = str(uuid.uuid4())
        now_iso = _utc_now_iso()

        if cache_hit:
            estimated_micro = 0
            cost_status: CostStatus = "zero_local"
        else:
            estimated_micro = estimate_operation_cost_micro_usd(provider, model, request_units, response_units)
            cost_status = "actual" if actual_micro_usd is not None else "estimated"

        with conn:
            conn.execute(
                """
                INSERT INTO usage_costs (
                    cost_id, library_id, job, analysis_revision, provider, model, operation,
                    request_units, response_units, started_at, finished_at, latency_ms,
                    cache_hit, status, safe_error, provider_request_id,
                    estimated_micro_usd, actual_micro_usd, cost_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cost_id,
                    library_id,
                    job,
                    analysis_revision,
                    provider,
                    model,
                    operation,
                    request_units,
                    response_units,
                    now_iso,
                    now_iso,
                    latency_ms,
                    1 if cache_hit else 0,
                    status,
                    safe_error,
                    provider_request_id,
                    estimated_micro,
                    actual_micro_usd,
                    cost_status,
                    now_iso,
                ),
            )

            # Settle budget reservation if one exists
            if reservation_id:
                conn.execute(
                    """
                    UPDATE budget_reservations
                    SET status = 'settled', settled_at = ?
                    WHERE reservation_id = ?
                    """,
                    (now_iso, reservation_id),
                )

        return cost_id


def get_cost_report(root: Path, period_type: str = "daily") -> dict[str, Any]:
    """Compile read-only financial and token usage report."""
    window = _current_period_window(period_type)
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)
        period_id = window.period_id

        cur = conn.cursor()
        cur.execute(
            """
            SELECT provider,
                   COUNT(*) as call_count,
                   SUM(request_units) as total_input_units,
                   SUM(response_units) as total_output_units,
                   SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as cache_hits,
                   SUM(estimated_micro_usd) as total_estimated_uUSD,
                   SUM(COALESCE(actual_micro_usd, 0)) as total_actual_uUSD
            FROM usage_costs
            WHERE created_at >= ? AND created_at < ?
            GROUP BY provider
            """,
            (window.start_utc.isoformat(), window.end_utc.isoformat()),
        )
        rows = cur.fetchall()

        provider_breakdown = []
        grand_total_uUSD = 0

        for r in rows:
            est_usd = round(r["total_estimated_uUSD"] / 1_000_000.0, 4)
            grand_total_uUSD += r["total_estimated_uUSD"]
            provider_breakdown.append(
                {
                    "provider": r["provider"],
                    "calls": r["call_count"],
                    "input_units": r["total_input_units"] or 0,
                    "output_units": r["total_output_units"] or 0,
                    "cache_hits": r["cache_hits"],
                    "estimated_usd": est_usd,
                }
            )

        return {
            "period": period_id,
            "period_type": period_type,
            "accounting_timezone": window.timezone,
            "period_start_utc": window.start_utc.isoformat(),
            "period_end_utc": window.end_utc.isoformat(),
            "grand_total_estimated_usd": round(grand_total_uUSD / 1_000_000.0, 4),
            "providers": provider_breakdown,
        }


def get_budget_status(root: Path) -> dict[str, Any]:
    """Return current budget usage vs limits."""
    window = _current_period_window("daily")
    period_id = window.period_id
    start_utc = window.start_utc.isoformat()
    end_utc = window.end_utc.isoformat()
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)
        daily_budget_usd = float(os.getenv("DAILY_BUDGET_USD", "0"))
        hard_stop = os.getenv("BUDGET_HARD_STOP", "false").lower() in ("true", "1", "yes")

        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(estimated_micro_usd), 0)
            FROM usage_costs
            WHERE created_at >= ? AND created_at < ? AND status != 'blocked_budget'
            """,
            (start_utc, end_utc),
        )
        spent_micro = int(cur.fetchone()[0])
        spent_usd = round(spent_micro / 1_000_000.0, 4)

        cur.execute(
            """
            SELECT COUNT(*)
            FROM usage_costs
            WHERE status = 'blocked_budget' AND created_at >= ? AND created_at < ?
            """,
            (start_utc, end_utc),
        )
        blocked_count = int(cur.fetchone()[0])

        remaining_usd = max(0.0, round(daily_budget_usd - spent_usd, 4)) if daily_budget_usd > 0 else None

        return {
            "period": period_id,
            "accounting_timezone": window.timezone,
            "period_start_utc": start_utc,
            "period_end_utc": end_utc,
            "daily_budget_limit_usd": daily_budget_usd if daily_budget_usd > 0 else "unlimited",
            "spent_usd": spent_usd,
            "remaining_usd": remaining_usd,
            "hard_stop_enabled": hard_stop,
            "blocked_operations_today": blocked_count,
        }


def get_provider_health(root: Path) -> dict[str, Any]:
    """Inspect provider error rates and latency stats."""
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        cur = conn.cursor()
        cur.execute(
            """
            SELECT provider,
                   COUNT(*) as total_calls,
                   SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) as successes,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failures,
                   AVG(latency_ms) as avg_latency_ms
            FROM usage_costs
            GROUP BY provider
            """
        )
        health = []
        for r in cur.fetchall():
            total = r["total_calls"] or 1
            success_rate = round((r["successes"] / total) * 100.0, 1)
            health.append(
                {
                    "provider": r["provider"],
                    "total_calls": total,
                    "success_rate_pct": success_rate,
                    "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
                    "status": "healthy" if success_rate >= 90.0 else "degraded",
                }
            )

        return {"providers": health}


def get_failed_list(root: Path) -> list[dict[str, Any]]:
    """Return recent failed operations and their safe errors."""
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        cur = conn.cursor()
        cur.execute(
            """
            SELECT cost_id, operation, provider, job, safe_error, created_at
            FROM usage_costs
            WHERE status IN ('failed', 'blocked_budget')
            ORDER BY created_at DESC LIMIT 50
            """
        )
        return [dict(r) for r in cur.fetchall()]
