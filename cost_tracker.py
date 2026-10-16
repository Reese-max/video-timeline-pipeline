"""API cost tracking, token accounting, and budget guard service (Issue #4).

Stores financial and usage records in operations.db.
Guarantees:
- Integer micro-USD storage ($1 = 1,000,000 micro-USD), prohibiting float drift in balances.
- Atomic budget reservations with settled + outstanding + proposed upper bounds.
- Status separation: HTTP outcome vs billing status (actual | estimated | confirmed_included | unknown).
- Pure local read-only cost reporting without triggering external paid network requests.
- `provider_cost_guard` wraps every enabled provider transport: reserve before the
  send, refuse the send when the budget is exhausted, settle afterwards.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from decimal import Decimal, InvalidOperation
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

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
                reservation_id TEXT,
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
        conn.execute("BEGIN IMMEDIATE")
        if "reservation_id" not in {row[1] for row in conn.execute("PRAGMA table_info(usage_costs)")}:
            conn.execute("ALTER TABLE usage_costs ADD COLUMN reservation_id TEXT")


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


def _daily_budget_usd() -> Decimal:
    """Parse the configured budget exactly and reject invalid or negative values."""
    raw = os.getenv("DAILY_BUDGET_USD", "0").strip()
    if not raw:
        raw = "0"
    try:
        budget = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("DAILY_BUDGET_USD must be a finite, non-negative decimal.") from exc
    if not budget.is_finite() or budget < 0:
        raise ValueError("DAILY_BUDGET_USD must be a finite, non-negative decimal.")
    return budget


def check_and_reserve_budget(
    root: Path,
    operation_name: str,
    estimated_micro_usd: int,
    account_scope: str = "default",
) -> tuple[bool, str | None, str]:
    """Check budget limits and atomically reserve funds before external network dispatch.

    Returns: (allowed, reservation_id, reason)
    """
    hard_stop = os.getenv("BUDGET_HARD_STOP", "false").strip().lower() in ("true", "1", "yes")
    daily_budget_usd = _daily_budget_usd()
    daily_budget_micro = int(daily_budget_usd * 1_000_000)

    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        period_id = _current_period_id("daily")

        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            # Sum settled actual/estimated costs for today
            cur.execute(
                """
                SELECT COALESCE(SUM(estimated_micro_usd), 0)
                FROM usage_costs AS usage
                WHERE account_scope = ? AND created_at LIKE ? AND status != 'blocked_budget'
                  AND NOT EXISTS (
                      SELECT 1 FROM budget_reservations AS reservation
                      WHERE reservation.reservation_id = usage.reservation_id
                        AND reservation.status = 'reserved'
                  )
                """,
                (account_scope, f"{period_id}%"),
            )
            settled_micro = int(cur.fetchone()[0])

            # Sum outstanding reservations
            cur.execute(
                """
                SELECT COALESCE(SUM(max_micro_usd), 0)
                FROM budget_reservations
                WHERE account_scope = ? AND status = 'reserved'
                """,
                (account_scope,),
            )
            outstanding_micro = int(cur.fetchone()[0])

            total_projected = settled_micro + outstanding_micro + estimated_micro_usd

            if hard_stop and daily_budget_usd > 0 and total_projected > daily_budget_micro:
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
                        payload={"reason": reason, "period_id": period_id, "account_scope": account_scope},
                        dedup_key=f"budget_blocked_{period_id}_{account_scope}",
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


class BudgetBlockedError(RuntimeError):
    """Raised before a paid transport is dispatched when the guard refuses it.

    A blocked call must never reach the provider: the transport raises this
    instead of opening the socket, so the reserved-but-unspent amount stays
    available for the operator's other work.
    """

    def __init__(self, reason: str, *, provider: str | None = None, operation: str | None = None) -> None:
        self.reason = reason
        self.provider = provider
        self.operation = operation
        super().__init__(reason)


_COST_ROOT: Path | None = None


def set_cost_root(root: Path | None) -> None:
    """Establish the project root whose operations.db backs the cost ledger."""
    global _COST_ROOT
    _COST_ROOT = Path(root) if root is not None else None


def get_cost_root() -> Path | None:
    return _COST_ROOT


@contextmanager
def cost_root_scope(root: Path | None) -> Iterator[Path | None]:
    """Bind the cost ledger root for the duration of one run or test."""
    previous = get_cost_root()
    set_cost_root(root)
    try:
        yield get_cost_root()
    finally:
        set_cost_root(previous)


def guard_is_strict() -> bool:
    """Strict mode: an operator asked for a hard stop with a positive daily cap.

    Outside strict mode the guard stays advisory, so the default local pipeline
    keeps working without a configured budget.
    """
    if os.getenv("BUDGET_HARD_STOP", "false").strip().lower() not in ("true", "1", "yes"):
        return False
    try:
        return _daily_budget_usd() > 0
    except ValueError as exc:
        raise BudgetBlockedError(
            "Budget blocked: DAILY_BUDGET_USD 設定無效，strict 模式 fail-closed。",
            provider="budget",
            operation="configuration",
        ) from exc


@dataclass
class GuardOutcome:
    """What the guard decided for one dispatched attempt."""

    provider: str
    operation: str
    reservation_id: str | None = None
    enforced: bool = False
    estimated_micro_usd: int = 0
    provider_operation_id: str | None = None
    request_units: int = 0
    response_units: int = 0
    sent: bool = True


def _record_guard_attempt(
    root: Path,
    outcome: GuardOutcome,
    *,
    model: str | None,
    status: UsageStatus,
    safe_error: str | None,
    request_units: int,
    response_units: int,
    latency_ms: float,
    account_scope: str,
    job: str | None,
    library_id: str | None,
    settle_reservation: bool,
) -> None:
    try:
        record_usage(
            root,
            provider=outcome.provider,
            operation=outcome.operation,
            job=job,
            library_id=library_id,
            model=model,
            request_units=request_units,
            response_units=response_units,
            latency_ms=latency_ms,
            status=status,
            safe_error=safe_error,
            provider_request_id=outcome.provider_operation_id,
            reservation_id=outcome.reservation_id,
            estimated_micro_usd=outcome.estimated_micro_usd,
            actual_micro_usd=0 if not outcome.sent else None,
            account_scope=account_scope,
            settle_reservation=settle_reservation,
        )
    except Exception as exc:  # telemetry must never break a paid attempt
        print(f"! 成本帳本寫入失敗（{outcome.provider}/{outcome.operation}）：{type(exc).__name__}")


@contextmanager
def provider_cost_guard(
    provider: str,
    operation: str,
    *,
    root: Path | None = None,
    model: str | None = None,
    request_units: int = 0,
    response_units: int = 0,
    estimated_micro_usd: int | None = None,
    account_scope: str = "default",
    job: str | None = None,
    library_id: str | None = None,
    provider_enforced_upper_bound_micro_usd: int | None = None,
) -> Iterator[GuardOutcome]:
    """Reserve budget before a paid transport send and settle the attempt after.

    Strict mode requires a provider-enforced upper bound, a ledger root, and
    available budget. A modelled estimate alone is not a hard bound and cannot
    authorize a paid transport in strict mode.
    """
    target = Path(root) if root is not None else get_cost_root()
    strict = guard_is_strict()

    if target is None:
        if strict:
            raise BudgetBlockedError(
                f"Budget blocked: 成本守衛尚未綁定專案根目錄（{provider}/{operation}）。",
                provider=provider,
                operation=operation,
            )
        yield GuardOutcome(provider=provider, operation=operation)
        return

    estimated_bound = (
        int(estimated_micro_usd)
        if estimated_micro_usd is not None
        else estimate_operation_cost_micro_usd(provider, model, request_units, response_units)
    )
    if estimated_bound < 0:
        raise ValueError("estimated_micro_usd must be non-negative.")

    if strict:
        if provider_enforced_upper_bound_micro_usd is None:
            raise BudgetBlockedError(
                f"Budget blocked: {provider}/{operation} 僅有估算、沒有供應商端可執行的費用上限，strict 模式拒絕送出。",
                provider=provider,
                operation=operation,
            )
        bound = int(provider_enforced_upper_bound_micro_usd)
        if bound <= 0:
            raise BudgetBlockedError(
                f"Budget blocked: {provider}/{operation} 的供應商費用上限無效，strict 模式拒絕送出。",
                provider=provider,
                operation=operation,
            )
    else:
        bound = estimated_bound

    try:
        allowed, reservation_id, reason = check_and_reserve_budget(
            target, operation_name=f"{provider}/{operation}", estimated_micro_usd=bound, account_scope=account_scope
        )
    except Exception as exc:
        # C3: a broken ledger must stop new paid I/O in hard mode instead of
        # silently letting the request through untracked.
        if strict:
            raise BudgetBlockedError(
                f"Budget blocked: 預算帳本不可用（{type(exc).__name__}），strict 模式 fail-closed。",
                provider=provider,
                operation=operation,
            ) from exc
        # 非嚴格模式是諮詢性的，但一定要讓操作者知道這次送出沒有被記帳。
        print(
            f"! 成本預留失敗（{type(exc).__name__}），非嚴格模式照常送出但不追蹤：{provider}/{operation}"
        )
        yield GuardOutcome(provider=provider, operation=operation, estimated_micro_usd=bound)
        return

    if not allowed:
        raise BudgetBlockedError(reason, provider=provider, operation=operation)

    outcome = GuardOutcome(
        provider=provider,
        operation=operation,
        reservation_id=reservation_id,
        enforced=True,
        estimated_micro_usd=bound,
        request_units=request_units,
        response_units=response_units,
    )
    started = time.monotonic()
    try:
        yield outcome
    except BaseException as exc:
        status_code = getattr(exc, "status_code", getattr(exc, "code", None))
        category = classify_error(
            exc,
            status_code if isinstance(status_code, int) else None,
            getattr(exc, "response_body", None),
        )
        # An unproven outcome (timeout, dropped connection, unknown) keeps its
        # reserved risk: settling it would let an unreconciled remote job look free.
        unresolved = outcome.sent and (
            getattr(exc, "outcome_unknown", False) or category in ("timeout", "network", "unknown")
        )
        if not outcome.sent:
            outcome.estimated_micro_usd = 0
        _record_guard_attempt(
            target,
            outcome,
            model=model,
            status="unknown" if unresolved else "failed",
            safe_error=category,
            request_units=request_units,
            response_units=response_units,
            latency_ms=(time.monotonic() - started) * 1000.0,
            account_scope=account_scope,
            job=job,
            library_id=library_id,
            settle_reservation=not unresolved,
        )
        raise
    _record_guard_attempt(
        target,
        outcome,
        model=model,
        status="succeeded",
        safe_error=None,
        request_units=outcome.request_units,
        response_units=outcome.response_units,
        latency_ms=(time.monotonic() - started) * 1000.0,
        account_scope=account_scope,
        job=job,
        library_id=library_id,
        settle_reservation=True,
    )


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
    estimated_micro_usd: int | None = None,
    account_scope: str = "default",
    settle_reservation: bool = True,
) -> str:
    """Record a completed or failed operation attempt and settle reservation."""
    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        cost_id = str(uuid.uuid4())
        now_iso = _utc_now_iso()

        if cache_hit:
            # 快取命中沒有新的雲端請求：任何傳入的估計都不適用（也不得回頭抵扣已花費用）。
            estimated_micro = 0
            cost_status: CostStatus = "zero_local"
        elif actual_micro_usd is not None:
            estimated_micro = int(actual_micro_usd)
            cost_status = "actual"
        elif estimated_micro_usd is not None:
            estimated_micro = int(estimated_micro_usd)
            # C2: an operation without a verified price is unknown cost, never free.
            cost_status = "estimated" if estimated_micro > 0 else "unknown"
        else:
            estimated_micro = estimate_operation_cost_micro_usd(provider, model, request_units, response_units)
            cost_status = "estimated" if estimated_micro > 0 else "unknown"

        with conn:
            conn.execute(
                """
                INSERT INTO usage_costs (
                    cost_id, library_id, job, analysis_revision, provider, model, operation,
                    request_units, response_units, started_at, finished_at, latency_ms,
                    cache_hit, status, safe_error, provider_request_id, reservation_id,
                    estimated_micro_usd, actual_micro_usd, cost_status, account_scope, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    reservation_id,
                    estimated_micro,
                    actual_micro_usd,
                    cost_status,
                    account_scope,
                    now_iso,
                ),
            )

            # Settle budget reservation if one exists
            if reservation_id and settle_reservation:
                conn.execute(
                    """
                    UPDATE budget_reservations
                    SET status = 'settled', settled_at = ?
                    WHERE reservation_id = ?
                    """,
                    (now_iso, reservation_id),
                )
            elif reservation_id:
                # Newly observed usage can increase risk even when the operation times out.
                conn.execute(
                    "UPDATE budget_reservations SET max_micro_usd = MAX(max_micro_usd, ?) "
                    "WHERE reservation_id = ? AND status = 'reserved'",
                    (estimated_micro, reservation_id),
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
