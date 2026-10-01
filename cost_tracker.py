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
import contextlib
import contextvars
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

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


class BudgetBlockedError(RuntimeError):
    """Raised before a provider transport is opened when the budget denies it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def get_operations_db(root: Path) -> sqlite3.Connection:
    """Late-importing wrapper; notifications imports this module for the guard."""
    from notifications import get_operations_db as _get_operations_db

    return _get_operations_db(root)


_COST_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "cost_tracker_root", default=None
)
_COST_ACCOUNT_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cost_tracker_account_scope", default="default"
)


@contextlib.contextmanager
def cost_tracking(root: Path, account_scope: str = "default"):
    """Enable guarded provider transports for the current call context."""
    root_token = _COST_ROOT.set(Path(root).resolve())
    scope_token = _COST_ACCOUNT_SCOPE.set(account_scope)
    try:
        yield
    finally:
        _COST_ACCOUNT_SCOPE.reset(scope_token)
        _COST_ROOT.reset(root_token)


def current_cost_root() -> Path | None:
    return _COST_ROOT.get()


def current_account_scope() -> str:
    return _COST_ACCOUNT_SCOPE.get()


def run_in_current_context(pool: Any, function: Any, *args: Any, **kwargs: Any) -> Any:
    """Submit work while preserving the active cost-guard context in a worker."""
    context = contextvars.copy_context()
    return pool.submit(context.run, function, *args, **kwargs)


def _host_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith("." + domain)


def provider_for_url(url: str) -> str:
    """Map known external hosts to the provider name used by the cost ledger."""
    hostname = (urllib_parse_hostname(url) or "").lower()
    if _host_matches(hostname, "groq.com"):
        return "groq"
    if _host_matches(hostname, "minimax.io") or _host_matches(hostname, "minimaxi.com"):
        return "minimax"
    if _host_matches(hostname, "openrouter.ai"):
        return "openrouter"
    if _host_matches(hostname, "apify.com"):
        return "apify"
    if _host_matches(hostname, "brightdata.com"):
        return "brightdata"
    if _host_matches(hostname, "graph.facebook.com"):
        return "meta"
    if _host_matches(hostname, "resend.com"):
        return "resend"
    if _host_matches(hostname, "googleapis.com"):
        return "google_drive"
    return "external"


def urllib_parse_hostname(url: str) -> str:
    # Keep this module independent from pipeline while still handling query strings safely.
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def estimate_transport_cost_micro_usd(provider: str, operation: str) -> int:
    """Return a conservative integer reservation for one network attempt."""
    normalized = operation.lower()
    if provider == "apify":
        return 50_000 if any(word in normalized for word in ("start", "trigger", "actor")) else 100
    if provider == "brightdata":
        return 50_000 if any(word in normalized for word in ("trigger", "submit")) else 100
    if provider in {"groq", "minimax", "resend", "email", "smtp", "webhook"}:
        return 1_000
    if provider == "openrouter":
        return 200
    if provider == "google_drive":
        return 100
    return 0


def _safe_transport_error(error: Exception) -> str:
    value = str(error)
    value = re.sub(r"(?i)bearer\s+\S+", "bearer <redacted>", value)
    value = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|key|password|secret)\s*[:=]\s*[^\s&,;'\"]+",
        r"\1=<redacted>",
        value,
    )
    return value[:500]


def mark_reservation_sent(root: Path, reservation_id: str) -> None:
    """Mark a reservation sent immediately before opening the external transport."""
    with closing(get_operations_db(root)) as conn:
        with conn:
            cursor = conn.execute(
                """
                UPDATE budget_reservations
                SET status = 'sent', sent_at = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (_utc_now_iso(), reservation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Cost guard reservation disappeared before transport dispatch.")


def guarded_call(
    provider: str,
    operation: str,
    function: Any,
    *args: Any,
    root: Path | None = None,
    model: str | None = None,
    request_units: int = 0,
    response_units: int = 0,
    estimated_micro_usd: int | None = None,
    account_scope: str | None = None,
    **kwargs: Any,
) -> Any:
    """Reserve, send, and record one provider transport attempt.

    The callback is never invoked when the hard budget blocks. Database errors
    before the callback are intentionally propagated so telemetry failure cannot
    turn into a fail-open paid request.
    """
    active_root = Path(root).resolve() if root is not None else current_cost_root()
    if active_root is None:
        return function(*args, **kwargs)

    scope = account_scope or current_account_scope()
    estimate = (
        estimate_transport_cost_micro_usd(provider, operation)
        if estimated_micro_usd is None
        else max(0, int(estimated_micro_usd))
    )
    allowed, reservation_id, reason = check_and_reserve_budget(
        active_root, operation_name=operation, estimated_micro_usd=estimate,
        account_scope=scope, provider=provider,
    )
    if not allowed:
        raise BudgetBlockedError(reason)
    if reservation_id is None:
        raise RuntimeError("Cost guard approved a transport without a reservation.")

    mark_reservation_sent(active_root, reservation_id)
    started = time.monotonic()
    try:
        result = function(*args, **kwargs)
    except Exception as error:
        category = classify_error(error)
        status: UsageStatus = "unknown" if category == "timeout" else "failed"
        try:
            record_usage(
                active_root,
                provider=provider,
                operation=operation,
                model=model,
                request_units=request_units,
                response_units=response_units,
                latency_ms=(time.monotonic() - started) * 1000,
                status=status,
                safe_error=_safe_transport_error(error),
                reservation_id=reservation_id,
            )
        except Exception:
            # The original provider error is more actionable; the sent
            # reservation remains non-reserved if settlement itself failed.
            pass
        raise

    record_usage(
        active_root,
        provider=provider,
        operation=operation,
        model=model,
        request_units=request_units,
        response_units=response_units,
        latency_ms=(time.monotonic() - started) * 1000,
        status="succeeded",
        reservation_id=reservation_id,
    )
    return result


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

    if isinstance(error, TimeoutError) or "timeout" in combined or "timed out" in combined or status_code in (408, 504):
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


def _current_period_id(period_type: str = "daily") -> str:
    tz_name = os.getenv("COST_TIMEZONE", "Asia/Taipei")
    now = datetime.datetime.now(datetime.timezone.utc)
    if period_type == "monthly":
        return now.strftime("%Y-%m")
    return now.strftime("%Y-%m-%d")


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
    provider: str = "system",
) -> tuple[bool, str | None, str]:
    """Check budget limits and atomically reserve funds before external network dispatch.

    Returns: (allowed, reservation_id, reason)
    """
    hard_stop = os.getenv("BUDGET_HARD_STOP", "false").lower() in ("true", "1", "yes")
    daily_budget_usd = float(os.getenv("DAILY_BUDGET_USD", "0"))
    daily_budget_micro = int(daily_budget_usd * 1_000_000)

    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        period_id = _current_period_id("daily")

        # BEGIN IMMEDIATE takes the write lock up front so concurrent workers
        # cannot read the same pre-reservation totals and all reserve past the
        # limit; the check and the INSERT run under one lock.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.cursor()
            # Sum settled actual/estimated costs for today
            cur.execute(
                """
                SELECT COALESCE(SUM(estimated_micro_usd), 0)
                FROM usage_costs
                WHERE account_scope = ? AND created_at LIKE ?
                  AND status NOT IN ('blocked_budget', 'unknown')
                """,
                (account_scope, f"{period_id}%"),
            )
            settled_micro = int(cur.fetchone()[0])

            # Sum outstanding reservations
            cur.execute(
                """
                SELECT COALESCE(SUM(max_micro_usd), 0)
                FROM budget_reservations
                WHERE account_scope = ? AND period_id = ? AND status IN ('reserved', 'sent', 'unknown')
                """,
                (account_scope, period_id),
            )
            outstanding_micro = int(cur.fetchone()[0])

            total_projected = settled_micro + outstanding_micro + estimated_micro_usd
            blocked_reason: str | None = None
            reservation_id: str | None = None

            if hard_stop and daily_budget_micro > 0 and total_projected > daily_budget_micro:
                blocked_reason = (
                    f"Budget blocked: projected ({total_projected / 1_000_000:.3f} USD) "
                    f"exceeds daily limit ({daily_budget_usd:.2f} USD)"
                )
                # Record blocked usage
                conn.execute(
                    """
                    INSERT INTO usage_costs (
                        cost_id, operation, provider, status, cost_status,
                        estimated_micro_usd, started_at, created_at, safe_error
                    ) VALUES (?, ?, ?, 'blocked_budget', 'zero_local', ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), operation_name, provider, estimated_micro_usd, _utc_now_iso(), _utc_now_iso(), blocked_reason),
                )
            else:
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
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

        if blocked_reason is not None:
            try:
                from notifications import record_event

                record_event(
                    conn,
                    event_type="budget.blocked",
                    library_id=None,
                    job=None,
                    analysis_revision=None,
                    payload={"reason": blocked_reason, "period_id": period_id, "account_scope": account_scope},
                    dedup_key=f"budget_blocked_{period_id}_{account_scope}",
                )
            except Exception:
                pass
            return False, None, blocked_reason

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

            # Unknown outcomes retain the reservation as outstanding until a
            # provider reconciliation can prove what was billed. Definite
            # success/failure releases the reservation into the usage ledger.
            if reservation_id:
                conn.execute(
                    """
                    UPDATE budget_reservations
                    SET status = ?, settled_at = CASE WHEN ? = 'unknown' THEN NULL ELSE ? END
                    WHERE reservation_id = ?
                    """,
                    ("unknown" if status == "unknown" else "settled", status, now_iso, reservation_id),
                )

        return cost_id


def get_cost_report(root: Path, period_type: str = "daily") -> dict[str, Any]:
    """Compile read-only financial and token usage report."""
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        period_id = _current_period_id(period_type)

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
            WHERE created_at LIKE ?
            GROUP BY provider
            """,
            (f"{period_id}%",),
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
            "grand_total_estimated_usd": round(grand_total_uUSD / 1_000_000.0, 4),
            "providers": provider_breakdown,
        }


def get_budget_status(root: Path) -> dict[str, Any]:
    """Return current budget usage vs limits."""
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        period_id = _current_period_id("daily")
        daily_budget_usd = float(os.getenv("DAILY_BUDGET_USD", "0"))
        hard_stop = os.getenv("BUDGET_HARD_STOP", "false").lower() in ("true", "1", "yes")

        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(estimated_micro_usd), 0)
            FROM usage_costs
            WHERE created_at LIKE ? AND status != 'blocked_budget'
            """,
            (f"{period_id}%",),
        )
        spent_micro = int(cur.fetchone()[0])
        spent_usd = round(spent_micro / 1_000_000.0, 4)

        cur.execute(
            """
            SELECT COUNT(*)
            FROM usage_costs
            WHERE status = 'blocked_budget' AND created_at LIKE ?
            """,
            (f"{period_id}%",),
        )
        blocked_count = int(cur.fetchone()[0])

        remaining_usd = max(0.0, round(daily_budget_usd - spent_usd, 4)) if daily_budget_usd > 0 else None

        return {
            "period": period_id,
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
