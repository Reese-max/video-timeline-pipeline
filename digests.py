"""Digest window calculation, candidate selection, and frozen payload generation.

Operates purely on local artifacts without calling external LLM APIs (zero-cost preview).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import operations


def parse_iso_datetime(value: str | None) -> datetime.datetime | None:
    """Parse ISO datetime string with robust timezone support."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def compute_digest_window(
    digest_type: str = "daily",
    as_of_date: str | None = None,
    tz_name: str = "Asia/Taipei",
    cutoff_hour: int = 20,
) -> tuple[datetime.datetime, datetime.datetime, str]:
    """Calculate [window_start, window_end) in UTC and return human-readable label.

    - Daily D: [D-1 20:00, D 20:00) in target timezone.
    - Weekly W: [Last Sunday 20:00, This Sunday 20:00) in target timezone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = datetime.timezone(datetime.timedelta(hours=8))  # fallback to UTC+8

    if as_of_date:
        ref_date = datetime.date.fromisoformat(as_of_date)
    else:
        ref_date = datetime.datetime.now(tz).date()

    if digest_type == "weekly":
        # Target end is Sunday 20:00 of the given week
        days_ahead = (6 - ref_date.weekday()) % 7  # 6 is Sunday
        end_date = ref_date + datetime.timedelta(days=days_ahead)
        window_end_local = datetime.datetime(
            end_date.year, end_date.month, end_date.day, cutoff_hour, 0, 0, tzinfo=tz
        )
        window_start_local = window_end_local - datetime.timedelta(days=7)
        label = f"截至 {end_date.isoformat()} {cutoff_hour:02d}:00（週報）"
    else:
        # Daily
        window_end_local = datetime.datetime(
            ref_date.year, ref_date.month, ref_date.day, cutoff_hour, 0, 0, tzinfo=tz
        )
        window_start_local = window_end_local - datetime.timedelta(days=1)
        label = f"截至 {ref_date.isoformat()} {cutoff_hour:02d}:00（日報）"

    window_start_utc = window_start_local.astimezone(datetime.timezone.utc)
    window_end_utc = window_end_local.astimezone(datetime.timezone.utc)
    return window_start_utc, window_end_utc, label


def collect_digest_candidates(
    root: Path,
    window_start_utc: datetime.datetime,
    window_end_utc: datetime.datetime,
) -> list[dict[str, Any]]:
    """Scan root/jobs/ for completed video artifacts created inside the time window."""
    jobs_dir = root / "jobs"
    if not jobs_dir.is_dir():
        return []

    # F0.2: when the completion registry exists it is the eligibility
    # authority. A registered revision with eligible_at NULL is reconciled
    # historical content and is not sent by default; jobs with no registry
    # row (or whose manifest revision was never registered) fall back to
    # manifest fields for backward compatibility — reconciliation
    # (--ops-reconcile) closes that gap. An unreadable registry fails closed:
    # better an empty digest than sending on guesswork.
    eligibility = operations.load_eligibility_index(root)
    if eligibility is None:
        return []

    candidates: list[dict[str, Any]] = []

    for job_path in jobs_dir.iterdir():
        if not job_path.is_dir():
            continue
        manifest_file = job_path / "manifest.json"
        summary_file = job_path / "summary.json"
        if not manifest_file.is_file() or not summary_file.is_file():
            continue

        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Check status: must be completed and not skipped
        if manifest.get("status") != "completed" or summary.get("status") == "skipped":
            continue

        # Check eligibility timestamp
        entry = eligibility.get(job_path.name)
        if entry is not None:
            revision = manifest.get("analysis_revision")
            if revision is None:
                registered_at = entry["latest"]
            elif str(revision) in entry["revisions"]:
                registered_at = entry["revisions"][str(revision)]
            else:
                # The manifest's content was never registered — treat as
                # unregistered and use the manifest fallback below.
                entry = None
        if entry is not None:
            if not registered_at:
                continue
            eligible_dt = parse_iso_datetime(registered_at)
            if not eligible_dt:
                continue
        else:
            eligible_str = (
                manifest.get("eligible_at")
                or manifest.get("created_at")
                or manifest.get("processing", {}).get("created_at")
            )
            eligible_dt = parse_iso_datetime(eligible_str)
            if not eligible_dt:
                # Fallback to filesystem mtime
                mtime = manifest_file.stat().st_mtime
                eligible_dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)

        if window_start_utc <= eligible_dt < window_end_utc:
            source = manifest.get("source", {})
            timeline_file = job_path / "timeline.json"
            chapters = []
            if timeline_file.is_file():
                try:
                    timeline_data = json.loads(timeline_file.read_text(encoding="utf-8"))
                    chapters = timeline_data.get("chapters", [])
                except Exception:
                    pass

            item = {
                "job": job_path.name,
                "title": source.get("title") or manifest.get("video", {}).get("title") or job_path.name,
                "url": source.get("url") or source.get("webpage_url") or "",
                "minutes": round(float(source.get("duration", 0)) / 60.0, 1),
                "summary": summary.get("brief") or summary.get("summary") or "",
                "terms": summary.get("terms", []),
                "chapters": chapters,
                "eligible_at": eligible_dt.isoformat(),
                "analysis_revision": manifest.get("analysis_revision", "1"),
            }
            candidates.append(item)

    # Sort candidates deterministically (e.g. by eligible_at descending)
    candidates.sort(key=lambda x: x["eligible_at"], reverse=True)
    return candidates


def build_digest_payload(
    candidates: list[dict[str, Any]],
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    window_label: str,
    digest_type: str = "daily",
    max_detailed: int = 8,
    max_brief: int = 12,
) -> dict[str, Any]:
    """Partition candidates into selected and deferred, and compute payload hash."""
    total_capacity = max_detailed + max_brief
    selected = candidates[:total_capacity]
    deferred = candidates[total_capacity:]

    payload = {
        "digest_type": digest_type,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_label": window_label,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "deferred_count": len(deferred),
        "selected_items": selected,
        "deferred_jobs": [item["job"] for item in deferred],
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    # Deterministic JSON hash
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    payload["rendered_payload_hash"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return payload
