"""API cost tracking, token accounting, and budget guard service (Issue #4).

Stores financial and usage records in operations.db.
Guarantees:
- Integer micro-USD storage ($1 = 1,000,000 micro-USD), prohibiting float drift in balances.
- Atomic budget reservations with settled + outstanding + proposed upper bounds.
- Status separation: HTTP outcome vs billing status (actual | estimated | confirmed_included | unknown | zero_rejected).
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
import zoneinfo
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from notifications import get_operations_db

CostStatus = Literal["actual", "estimated", "confirmed_included", "unknown", "zero_local", "zero_rejected"]
# The provider answered and refused the request before doing any work (rate limited or
# overloaded). Field test c9289f71a482 booked four MiniMax 529s at the full reservation.
REJECTED_BEFORE_WORK_STATUS = {429, 529}
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
                reservation_id TEXT,
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
            -- Outstanding reservations are selected by the configured period
            -- window, so they are indexed by the same columns the filter uses.
            -- period_id is kept as a stored audit label (reporting, dedup key)
            -- but is no longer a predicate, so ix_budget_period is not created.
            CREATE INDEX IF NOT EXISTS ix_budget_reserved_window
                ON budget_reservations(account_scope, created_at, status);
            """
        )
        # Older operations.db files predate the attempt/reservation link. Keep
        # their rows intact; only new records receive an explicit link.
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_costs)")}
        if "reservation_id" not in columns:
            conn.execute("ALTER TABLE usage_costs ADD COLUMN reservation_id TEXT")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _cost_timezone() -> zoneinfo.ZoneInfo:
    """Resolve COST_TIMEZONE to an IANA timezone; invalid values fail explicitly."""
    tz_name = (os.getenv("COST_TIMEZONE", "Asia/Taipei") or "").strip()
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        # A missing system tz database and a misspelled key raise the same
        # exception; name the remedy so an operator does not keep editing a
        # valid setting on a host that simply has no IANA data installed.
        raise ValueError(
            f"Invalid COST_TIMEZONE {tz_name!r}: {exc}. "
            "Use an IANA zone name (e.g. Asia/Taipei, UTC); if the name is "
            "correct, install the IANA database (pip install tzdata)."
        ) from exc


def _current_period(period_type: str = "daily") -> tuple[str, str, str, str]:
    """Return (period_id, window_start_utc_iso, window_end_utc_iso, tz_key).

    The period is the current local day/month in the configured COST_TIMEZONE;
    the bounds are the UTC instants of that local window so created_at filters
    attribute spend to the configured accounting period, not the UTC date.
    """
    tz = _cost_timezone()
    local_today = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).date()
    if period_type == "monthly":
        start_date = local_today.replace(day=1)
        if start_date.month == 12:
            end_date = start_date.replace(year=start_date.year + 1, month=1)
        else:
            end_date = start_date.replace(month=start_date.month + 1)
        period_id = start_date.strftime("%Y-%m")
    else:
        start_date = local_today
        end_date = local_today + datetime.timedelta(days=1)
        period_id = local_today.strftime("%Y-%m-%d")
    start_utc = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz).astimezone(datetime.timezone.utc)
    end_utc = datetime.datetime.combine(end_date, datetime.time.min, tzinfo=tz).astimezone(datetime.timezone.utc)
    # timespec="seconds" pins the bound format; stored created_at values compare
    # correctly whether or not they carry a fractional-second component.
    return period_id, start_utc.isoformat(timespec="seconds"), end_utc.isoformat(timespec="seconds"), tz.key


def _current_period_id(period_type: str = "daily") -> str:
    return _current_period(period_type)[0]


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


def _hard_stop_enabled() -> bool:
    return os.getenv("BUDGET_HARD_STOP", "false").strip().lower() in ("true", "1", "yes")


def _budget_balances(conn: sqlite3.Connection, start: str, end: str,
                     account_scope: str | None = None) -> tuple[int, int]:
    """Read settled spend and outstanding risk from one caller-owned snapshot."""
    settled = conn.execute(
        """
        SELECT COALESCE(SUM(u.estimated_micro_usd), 0)
        FROM usage_costs u
        WHERE (? IS NULL OR u.account_scope = ?) AND u.created_at >= ? AND u.created_at < ?
          AND u.status != 'blocked_budget'
          AND NOT EXISTS (
            SELECT 1 FROM budget_reservations b
            WHERE b.reservation_id = u.reservation_id AND b.status = 'reserved'
          )
        """, (account_scope, account_scope, start, end),
    ).fetchone()[0]
    # Use the configured period's UTC window for both halves, including when
    # COST_TIMEZONE has changed since the original reservation was created.
    outstanding = conn.execute(
        """
        SELECT COALESCE(SUM(max_micro_usd), 0) FROM budget_reservations
        WHERE (? IS NULL OR account_scope = ?) AND created_at >= ? AND created_at < ?
          AND status = 'reserved'
        """, (account_scope, account_scope, start, end),
    ).fetchone()[0]
    return int(settled), int(outstanding)


def check_and_reserve_budget(
    root: Path,
    operation_name: str,
    estimated_micro_usd: int,
    account_scope: str = "default",
) -> tuple[bool, str | None, str]:
    """Check budget limits and atomically reserve funds before external network dispatch.

    Returns: (allowed, reservation_id, reason)
    """
    hard_stop = _hard_stop_enabled()
    daily_budget_usd = _daily_budget_usd()
    daily_budget_micro = int(daily_budget_usd * 1_000_000)

    # Resolve the accounting period first: an invalid COST_TIMEZONE must fail
    # before get_operations_db creates/migrates operations.db.
    period_id, period_start_utc, period_end_utc, tz_key = _current_period("daily")

    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        with conn:
            # SELECT does not begin SQLite's implicit write transaction. Take
            # the writer lease before reading either balance so another worker
            # cannot approve against the same pre-reservation balance.
            conn.execute("BEGIN IMMEDIATE")
            settled_micro, outstanding_micro = _budget_balances(
                conn, period_start_utc, period_end_utc, account_scope,
            )

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
                        payload={"reason": reason, "period_id": period_id, "account_scope": account_scope, "accounting_timezone": tz_key},
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
    if not _hard_stop_enabled():
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


@dataclass
class _ActiveAttempt:
    root: Path
    outcome: GuardOutcome
    model: str | None
    request_units: int
    response_units: int
    account_scope: str
    job: str | None
    library_id: str | None
    logical_operation_id: str
    started: float
    attempt_no: int = 1
    transport_used: bool = False
    recorded: bool = False


_ACTIVE_ATTEMPT: ContextVar[_ActiveAttempt | None] = ContextVar("provider_cost_attempt", default=None)


def _finish_attempt(state: _ActiveAttempt, error: BaseException | None = None) -> None:
    if state.recorded:
        return
    category = None
    unresolved = False
    rejected = False
    if error is not None:
        status_code = getattr(error, "status_code", getattr(error, "code", None))
        category = classify_error(error, status_code if isinstance(status_code, int) else None,
                                  getattr(error, "response_body", None))
        # A failed trigger response does not prove that a remote job was never
        # accepted. Keep the same risk as an uncertain POST timeout.
        remote_trigger = (state.outcome.provider, state.outcome.operation) in {
            ("apify", "actor.run.start"), ("brightdata", "datasets.trigger")
        }
        unresolved = (category in ("timeout", "network", "unknown")
                      or getattr(error, "provider_outcome_unknown", False)
                      or (remote_trigger and isinstance(status_code, int)
                          and (status_code == 408 or status_code >= 500)))
        rejected = not unresolved and isinstance(status_code, int) and status_code in REJECTED_BEFORE_WORK_STATUS
    _record_guard_attempt(
        state.root, state.outcome, model=state.model,
        status=("unknown" if unresolved else "failed") if error is not None else "succeeded",
        safe_error=category, request_units=state.request_units, response_units=state.response_units,
        latency_ms=(time.monotonic() - state.started) * 1000.0,
        account_scope=state.account_scope, job=state.job, library_id=state.library_id,
        settle_reservation=not unresolved, logical_operation_id=state.logical_operation_id,
        attempt_no=state.attempt_no, provider_rejected=rejected,
    )
    state.recorded = True


@contextmanager
def provider_transport_attempt() -> Iterator[None]:
    """Own one actual request inside an existing provider guard.

    Reuse its first reservation. Each retry gets a new reservation before I/O;
    failures are recorded before the caller decides whether to retry. The last
    successful attempt is recorded when the outer guard has captured remote IDs.
    Unrelated HTTP helpers remain outside this provider-specific context.
    """
    state = _ACTIVE_ATTEMPT.get()
    if state is None:
        yield
        return
    if state.transport_used:
        # A guard can enclose several requests (e.g. replace + upload). Finish
        # the previous successful request before reserving the next one.
        _finish_attempt(state)
        try:
            allowed, reservation_id, reason = check_and_reserve_budget(
                state.root, f"{state.outcome.provider}/{state.outcome.operation}",
                state.outcome.estimated_micro_usd, state.account_scope,
            )
        except Exception as exc:
            if guard_is_strict():
                raise BudgetBlockedError(
                    f"Budget blocked: 重試預算帳本不可用（{type(exc).__name__}），strict 模式 fail-closed。",
                    provider=state.outcome.provider, operation=state.outcome.operation,
                ) from exc
            print(f"! 成本預留失敗（{type(exc).__name__}），非嚴格模式重試未追蹤。")
            allowed, reservation_id, reason = True, None, "advisory ledger unavailable"
        if not allowed:
            raise BudgetBlockedError(reason, provider=state.outcome.provider, operation=state.outcome.operation)
        state.outcome.reservation_id = reservation_id
        state.outcome.provider_operation_id = None
        state.attempt_no += 1
        state.started = time.monotonic()
        state.recorded = False
    state.transport_used = True
    try:
        yield
    except BaseException as exc:
        _finish_attempt(state, exc)
        raise


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
    logical_operation_id: str | None = None,
    attempt_no: int = 1,
    provider_rejected: bool = False,
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
            account_scope=account_scope,
            settle_reservation=settle_reservation,
            logical_operation_id=logical_operation_id,
            attempt_no=attempt_no,
            provider_rejected=provider_rejected,
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
    )
    state = _ActiveAttempt(target, outcome, model, request_units, response_units, account_scope,
                           job, library_id, str(uuid.uuid4()), time.monotonic())
    token = _ACTIVE_ATTEMPT.set(state)
    try:
        yield outcome
    except BaseException as exc:
        _finish_attempt(state, exc)
        raise
    else:
        _finish_attempt(state)
    finally:
        _ACTIVE_ATTEMPT.reset(token)


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
    logical_operation_id: str | None = None,
    attempt_no: int = 1,
    provider_rejected: bool = False,
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
        elif provider_rejected:
            # 供應商明確拒絕（429／529）時沒有做任何工作；保留紀錄但不佔預算。已知實際費用仍以上一支為準。
            estimated_micro = 0
            cost_status = "zero_rejected"
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
                    cache_hit, status, safe_error, provider_request_id,
                    estimated_micro_usd, actual_micro_usd, cost_status, account_scope, created_at,
                    reservation_id, logical_operation_id, attempt_no
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    account_scope,
                    now_iso,
                    reservation_id,
                    logical_operation_id,
                    attempt_no,
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

        return cost_id


def record_provider_cache_hit(provider: str, operation: str, *, model: str | None = None,
                              job: str | None = None, root: Path | None = None) -> None:
    """Record reuse without reserving funds, sending I/O, or undoing past spend."""
    target = root if root is not None else get_cost_root()
    if target is None:
        return
    try:
        record_usage(target, provider=provider, operation=operation, model=model,
                     job=job, cache_hit=True, status="cache_hit")
    except Exception as exc:
        # An unavailable ledger must not prevent a proven local cache read.
        print(f"! 快取成本紀錄失敗（{type(exc).__name__}）：{provider}/{operation}")


def get_cost_report(root: Path, period_type: str = "daily") -> dict[str, Any]:
    """Compile read-only financial and token usage report."""
    period_id, period_start_utc, period_end_utc, tz_key = _current_period(period_type)

    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

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
            WHERE created_at >= ? AND created_at < ? AND status != 'blocked_budget'
            GROUP BY provider
            """,
            (period_start_utc, period_end_utc),
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
            "accounting_timezone": tz_key,
            "grand_total_estimated_usd": round(grand_total_uUSD / 1_000_000.0, 4),
            "providers": provider_breakdown,
        }


def get_budget_status(root: Path) -> dict[str, Any]:
    """Return the root ledger's settled spend, outstanding risk and availability."""
    period_id, period_start_utc, period_end_utc, tz_key = _current_period("daily")
    daily_budget = _daily_budget_usd()
    daily_budget_usd = float(daily_budget)
    daily_budget_micro = int(daily_budget * 1_000_000)
    hard_stop = _hard_stop_enabled()

    with closing(get_operations_db(root)) as conn:
        _init_cost_schema(conn)

        with conn:
            # Both balance reads share one snapshot, including a concurrent
            # worker's atomic usage insert + reservation settlement.
            conn.execute("BEGIN")
            spent_micro, outstanding_micro = _budget_balances(conn, period_start_utc, period_end_utc)
            blocked_count = int(conn.execute(
                """SELECT COUNT(*) FROM usage_costs
                   WHERE status = 'blocked_budget' AND created_at >= ? AND created_at < ?""",
                (period_start_utc, period_end_utc),
            ).fetchone()[0])
        projected_micro = spent_micro + outstanding_micro
        available_micro = max(0, daily_budget_micro - projected_micro) if daily_budget > 0 else None
        remaining_usd = available_micro / 1_000_000.0 if available_micro is not None else None

        return {
            "period": period_id,
            "accounting_timezone": tz_key,
            "daily_budget_limit_usd": daily_budget_usd if daily_budget_usd > 0 else "unlimited",
            "account_scope": "all_root_ledger_scopes",
            "spent_usd": round(spent_micro / 1_000_000.0, 4),
            "outstanding_usd": outstanding_micro / 1_000_000.0,
            "projected_usd": projected_micro / 1_000_000.0,
            "remaining_usd": remaining_usd,
            "spent_micro_usd": spent_micro,
            "outstanding_micro_usd": outstanding_micro,
            "projected_micro_usd": projected_micro,
            "available_micro_usd": available_micro,
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
