"""Shared operation budgets; worker process groups provide the hard job boundary."""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Any


class DeadlineExceeded(RuntimeError):
    """The operation or enclosing video job has exhausted its time budget."""


class ExecutionCancelled(RuntimeError):
    """Cooperative cancellation requested by an embedding worker."""


_cancelled = threading.Event()


def cancel_execution() -> None:
    _cancelled.set()


def reset_execution_cancellation() -> None:
    """Only call between jobs, never while operations are running."""
    _cancelled.clear()


def _positive_number(value: str | float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


class OperationBudget:
    def __init__(self, label: str, default_seconds: float, env_name: str) -> None:
        self.label = label
        seconds = _positive_number(os.environ.get(env_name, default_seconds), env_name)
        job_deadline = os.environ.get("VIDEO_JOB_DEADLINE", "").strip()
        if job_deadline:
            absolute = _positive_number(job_deadline, "VIDEO_JOB_DEADLINE")
            seconds = min(seconds, absolute - time.time())
        # Convert the wall-clock job deadline once; subsequent checks are monotonic.
        self.deadline = time.monotonic() + seconds
        self.check()

    def check(self) -> None:
        if _cancelled.is_set():
            raise ExecutionCancelled(f"{self.label} cancelled")
        if time.monotonic() >= self.deadline:
            raise DeadlineExceeded(f"{self.label} exceeded its time budget")

    def remaining(self, limit: float | None = None) -> float:
        self.check()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded(f"{self.label} exceeded its time budget")
        return min(remaining, limit) if limit is not None else remaining

    def sleep(self, seconds: float) -> None:
        # Refuse a retry whose server-requested backoff cannot fit in this budget.
        if seconds >= self.remaining():
            raise DeadlineExceeded(f"{self.label} retry would exceed its time budget")
        time.sleep(max(0.0, seconds))
        self.check()


def read_response(response: Any, budget: OperationBudget, limit: int | None = None, *, read_timeout: float | None = None) -> bytes:
    """Read real HTTP streams in bounded chunks and re-check the shared deadline.

    urlopen/DNS cannot be forcibly cancelled by Python threads. The job supervisor
    must still terminate its process group at the absolute VIDEO_JOB_DEADLINE.
    """
    budget.check()
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        raw = response.read(limit) if limit is not None else response.read()
        budget.check()
        return raw
    chunks: list[bytes] = []
    length = 0
    while limit is None or length < limit:
        remaining = budget.remaining(read_timeout)
        # CPython's HTTPResponse socket; absent on custom transports/test doubles.
        raw_stream = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(raw_stream, "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read1(min(65536, limit - length) if limit is not None else 65536)
        budget.check()
        if not chunk:
            break
        chunks.append(chunk)
        length += len(chunk)
    return b"".join(chunks)
