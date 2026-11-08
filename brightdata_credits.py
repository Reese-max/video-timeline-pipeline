"""Shared UTC credit ledger, independent of the USD cost guard and job roots."""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from cost_tracker import BudgetBlockedError


class BrightDataQuotaError(BudgetBlockedError):
    """Only raised by the ledger before a new trigger can be submitted."""

    def __init__(self, message: str, resets_at: str):
        super().__init__(message, provider="brightdata", operation="datasets.trigger")
        self.resets_at = resets_at


def _limit(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{name} 必須是非負整數；已禁止 Bright Data 請求。")
    return int(value)


def ledger_path() -> Path:
    configured = os.getenv("BRIGHTDATA_CREDIT_LEDGER", "").strip()
    return (Path(configured).expanduser() if configured else
            Path.home() / ".local/state/video-timeline-pipeline/brightdata-credits.sqlite3")


def limits() -> tuple[int, int]:
    return (_limit("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", 4500),
            _limit("BRIGHTDATA_POST_DAILY_LIMIT", 20))


def _connect() -> sqlite3.Connection:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.getenv("BRIGHTDATA_CREDIT_LEDGER", "").strip():
        path.parent.chmod(0o700)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("""CREATE TABLE IF NOT EXISTS reservations (
        id TEXT PRIMARY KEY, month TEXT NOT NULL, day TEXT NOT NULL,
        credits INTEGER NOT NULL, actual INTEGER, daily_post INTEGER NOT NULL)""")
    return connection


def _utc(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)


def _usage(connection: sqlite3.Connection, now: dt.datetime, limit: int, daily_limit: int) -> dict:
    month, day = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    used, reserved = connection.execute(
        "SELECT COALESCE(SUM(actual),0), COALESCE(SUM(CASE WHEN actual IS NULL THEN credits ELSE 0 END),0) "
        "FROM reservations WHERE month=?", (month,)).fetchone()
    daily = connection.execute(
        "SELECT COALESCE(SUM(daily_post),0) FROM reservations WHERE day=?", (day,)).fetchone()[0]
    return {"month": month, "limit": limit, "used": used, "reserved": reserved,
            "remaining": max(0, limit - used - reserved), "daily_posts_used": daily,
            "daily_post_limit": daily_limit}


def usage(*, now: dt.datetime | None = None) -> dict:
    limit, daily_limit = limits()
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN")
        return _usage(connection, _utc(now), limit, daily_limit)


def reserve(credits: int, *, daily_post: bool = False, now: dt.datetime | None = None,
            reservation_id: str | None = None) -> str:
    if isinstance(credits, bool) or not isinstance(credits, int) or credits <= 0:
        raise ValueError("Bright Data 預留 credit 數必須是正整數。")
    limit, daily_limit = limits()
    now = _utc(now)
    reservation_id = reservation_id or uuid.uuid4().hex
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        # A receipt directory can race across processes. An orphan reservation is uncertain,
        # not permission to send again (even if someone removed its marker).
        if connection.execute("SELECT 1 FROM reservations WHERE id=?", (reservation_id,)).fetchone():
            raise RuntimeError("Bright Data 已有預留；禁止重送，請核對回執與帳本。")
        state = _usage(connection, now, limit, daily_limit)
        next_month = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        reset = next_month.strftime("%Y-%m-%dT%H:%M:%SZ")
        monthly = (f"Bright Data 本月已用 {state['used']}、已預留 {state['reserved']}／上限 {limit} credits；"
                   f"每月重置日 {reset}（UTC）。")
        if state["used"] + state["reserved"] + credits > limit:
            raise BrightDataQuotaError(monthly + "本次額度不足，未送出請求。", reset)
        if daily_post and state["daily_posts_used"] >= daily_limit:
            daily_reset = (now + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            raise BrightDataQuotaError(
                monthly + f"今日新貼文採集已用 {state['daily_posts_used']}／每日上限 {daily_limit}；"
                f"每日重置日 {daily_reset}（UTC），未送出請求。", daily_reset)
        connection.execute("INSERT INTO reservations VALUES (?,?,?,?,NULL,?)",
                           (reservation_id, state["month"], now.strftime("%Y-%m-%d"), credits, int(daily_post)))
    return reservation_id


def release(reservation_id: str) -> None:
    """Release only an unsettled reservation for a trigger known not to have been sent."""
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM reservations WHERE id=? AND actual IS NULL", (reservation_id,))


def settle(reservation_id: str, actual: int) -> None:
    if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
        raise ValueError("Bright Data 實際 credit 數必須是非負整數。")
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM reservations WHERE id=?", (reservation_id,)).fetchone():
            raise RuntimeError("Bright Data 預留不存在；請核對共用帳本設定。")
        connection.execute("UPDATE reservations SET actual=? WHERE id=? AND actual IS NULL", (actual, reservation_id))
