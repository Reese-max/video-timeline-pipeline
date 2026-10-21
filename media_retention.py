"""Delete only expired, successfully analyzed MCP media; dry-run by default.

Jobs whose source and segments are verified on Google Drive are cleaned soon
after analysis (Drive is the long-term copy and restores them on demand); other
jobs keep the 7-day rule.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path

import drive_storage
import pipeline
from media_lifecycle import StoragePolicy


MEDIA = re.compile(
    r"(?:source\.(?:mp4|mkv|webm|mov|avi|m4v)|threads_media_\d+\.mp4|"
    r"segments/segment_\d+\.mp4|previews/preview_\d+\.jpg|"
    r"analysis/(?:runs/[a-f0-9]{64}/)?(?:frames|budgeted-frames)/frame_\d+\.jpg|"
    r"analysis/(?:runs/[a-f0-9]{64}/)?audio/audio_\d+\.mp3|"
    r"analysis/runs/[a-f0-9]{64}/vision-video-work/minimax-compressed\.mp4)", re.I)
AUDIT = "media-retention.json"
# Media rebuilt from the source (previews, analysis frames/audio, compression output).
DERIVED = re.compile(
    r"(?:previews/preview_\d+\.jpg|"
    r"analysis/(?:runs/[a-f0-9]{64}/)?(?:frames|budgeted-frames)/frame_\d+\.jpg|"
    r"analysis/(?:runs/[a-f0-9]{64}/)?audio/audio_\d+\.mp3|"
    r"analysis/runs/[a-f0-9]{64}/vision-video-work/minimax-compressed\.mp4|"
    r"threads_media_\d+\.mp4)", re.I)


def drive_backup_still_verified(job: Path) -> bool:
    """Re-check Drive right before the last local copy of the source goes."""
    try:
        state = json.loads((job / "drive-manifest.json").read_text(encoding="utf-8"))
        return bool(drive_storage.verify_remote_backup(state))
    except Exception:
        return False


def drive_backed_grace_seconds() -> int:
    """Quiet period after the last preparation/analysis before Drive-backed media goes."""
    raw = os.getenv("MCP_DRIVE_BACKED_GRACE_SECONDS", "").strip()
    return max(0, int(raw)) if raw else 3600


def _selected_media(job: Path, files: list[Path], source: Path) -> tuple[list[Path], str]:
    selected = [p for p in files if MEDIA.fullmatch(p.relative_to(job).as_posix())]
    if any(not stat.S_ISREG(p.lstat().st_mode) or p.stat().st_nlink != 1 for p in selected):
        return [], "linked_or_nonregular_media"
    # Keep the source until everything else has been removed, so interrupted runs can resume.
    return sorted(selected, key=lambda p: (p == source, str(p))), "eligible"


def drive_backed_media(job: Path, files: list[Path], now: float) -> tuple[list[Path], str]:
    """Media of an analyzed job whose source and segments are verified on Drive."""
    try:
        marker = StoragePolicy._marker(job)
        if not marker:
            return [], "unmanaged"
        # Links issued before link_until existed were only recorded in protected_until.
        link_guard = marker["protected_until"] if marker.get("link_until") is None else marker["link_until"]
        if now < float(link_guard):
            return [], "retained_or_live_share"
        if now - max(marker["managed_at"], marker["last_used_at"]) < drive_backed_grace_seconds():
            return [], "younger_than_retention"
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        result = json.loads((job / "analysis/result.json").read_text(encoding="utf-8"))
        fingerprint = manifest["source"]["fingerprint"]
        # Any finished analysis of this exact source counts: Drive restores it for re-analysis.
        if (manifest["status"] != "ready" or manifest["job_id"] != job.name
                or result["job_id"] != job.name or result.get("source_fingerprint") != fingerprint):
            return [], "not_complete_or_needs_review"
        state = json.loads((job / "drive-manifest.json").read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("job_id") != job.name
                or not drive_storage.backup_complete(state, manifest["segments"], fingerprint)):
            return [], "drive_backup_incomplete"
        try:
            source = pipeline.locate_source_video(job)
        except RuntimeError:
            source = job / state["source_file"]["file_name"]
        if not drive_storage.local_matches_backup(job, state):
            return [], "source_changed"
        backed_up = drive_storage.backup_relative_paths(state)
        for path in files:
            relative = path.relative_to(job).as_posix()
            if MEDIA.fullmatch(relative) and relative not in backed_up and not DERIVED.fullmatch(relative):
                return [], "unbacked_media"  # e.g. a newer source the backup never saw
        try:
            verified = drive_storage.verify_remote_backup(state)
        except Exception:  # Unknown Drive state never authorizes a deletion.
            verified = False
        if not verified:
            return [], "drive_unverified"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return [], "missing_or_invalid_evidence"
    paths, reason = _selected_media(job, files, source)
    return paths, ("eligible_drive_backed" if paths else reason)


def eligible_media(job: Path, cutoff: float, now: float | None = None) -> tuple[list[Path], str]:
    if job.is_symlink() or getattr(job, "is_junction", lambda: False)():
        return [], "linked_path"
    paths = list(job.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in [job, *paths]):
        return [], "linked_path"
    files = [p for p in paths if p.is_file()]
    if any(p.name == ".keep-media" or p.suffix in {".tmp", ".part", ".ytdl"} for p in files):
        return [], "review_or_incomplete"
    if (job / "drive-manifest.json").exists():
        return drive_backed_media(job, files, time.time() if now is None else now)
    try:
        marker = StoragePolicy._marker(job)
        if marker and (max(marker["managed_at"], marker["last_used_at"]) >= cutoff
                       or marker["protected_until"] > cutoff + 7 * 86400):
            return [], "retained_or_live_share"
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        result_path = job / "analysis/result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (manifest["status"] != "ready" or manifest["job_id"] != job.name
                or result["status"] != "complete" or result["job_id"] != job.name
                or result.get("errors") or result["quality"]["status"] != "evidence_available"
                or result["quality"].get("warnings")
                or any(pipeline.saved_visual_timestamp_issue(row, result.get("duration_seconds"))
                       or not pipeline.whole_video_observation(row.get("data"))
                       for row in result.get("timeline", []) if row.get("type") == "visual")):
            return [], "not_complete_or_needs_review"
        source = pipeline.locate_source_video(job)
        if not source.is_file() or result["source_fingerprint"] != manifest["source"]["fingerprint"]:
            return [], "source_changed"
        if pipeline.file_fingerprint(source) != result["source_fingerprint"]:
            return [], "source_changed"
        if result["source_sha256"] != pipeline.file_sha256(source):
            return [], "source_changed"
        # result.json is written only on completed analysis. Later writes extend retention.
        if max(p.stat().st_mtime for p in files if p.name != AUDIT) >= cutoff:
            return [], "younger_than_retention"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return [], "missing_or_invalid_evidence"
    return _selected_media(job, files, source)


def cleanup(jobs_root: Path, *, apply: bool = False, now: float | None = None) -> dict:
    root = jobs_root.absolute()
    if root.name != "mcp_jobs" or not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError("清理目標必須是明確、既有且無連結的 mcp_jobs 絕對路徑。")
    timestamp = time.time() if now is None else now
    report = {"mode": "apply" if apply else "dry_run", "retention_days": 7,
              "eligible_jobs": 0, "drive_backed_jobs": 0, "selected_files": 0, "selected_bytes": 0,
              "deleted_files": 0, "deleted_bytes": 0, "skipped": collections.Counter(), "errors": []}
    with (pipeline.file_writer_lock(root / ".writer.lock"),
          pipeline.file_writer_lock(root / ".media-lifecycle.lock"),
          contextlib.ExitStack() as stack):
        protected = set()
        database = root.parent / ".video-state" / "jobs.sqlite3"
        if database.exists():
            if database.resolve() != database or not database.is_file():
                raise ValueError("Queue database must be an ordinary non-linked file")
            db = stack.enter_context(contextlib.closing(sqlite3.connect(
                database.as_uri() + "?mode=rw", uri=True, timeout=0)))
            try:
                # Serialize enqueue/status changes with the complete cleanup, without changing rows.
                db.execute("BEGIN IMMEDIATE")
                for job_id, status in db.execute("SELECT job_id,status FROM jobs WHERE status!='completed'"):
                    if status in {"queued", "running"} or job_id is None:
                        report["skipped"]["queue_active_or_unresolved"] += 1
                        return report
                    protected.add(job_id)
            except sqlite3.OperationalError as exc:
                raise pipeline.WriterBusyError("Queue state unavailable; no media removed") from exc
        for job in sorted(root.iterdir()):
            if not re.fullmatch(r"[a-f0-9]{12}", job.name) or not job.is_dir():
                continue
            if job.name in protected:
                report["skipped"]["queue_failure_or_review"] += 1
                continue
            try:
                paths, reason = eligible_media(job, timestamp - 7 * 86400, timestamp)
                if not paths:
                    report["skipped"][reason] += 1
                    continue
                if reason == "eligible_drive_backed":
                    report["drive_backed_jobs"] += 1
                snapshots = {p: p.lstat() for p in paths}
                report["eligible_jobs"] += 1
                report["selected_files"] += len(paths)
                report["selected_bytes"] += sum(s.st_size for s in snapshots.values())
                if not apply:
                    continue
                audit = {"status": "deleting", "started_at": timestamp, "retention_days": 7,
                         "policy": "drive_backed" if reason == "eligible_drive_backed" else "retention_days",
                         "planned_files": [str(p.relative_to(job)) for p in paths], "deleted_files": []}
                pipeline.write_json(job / AUDIT, audit)
                for path in paths:
                    if (reason == "eligible_drive_backed" and path.parent == job and path.name.startswith("source.")
                            and not drive_backup_still_verified(job)):
                        audit.update(status="stopped_drive_changed", completed_at=time.time())
                        pipeline.write_json(job / AUDIT, audit)
                        raise RuntimeError("Google Drive 備份在清理途中變動；保留原片。")
                    old, current = snapshots[path], path.lstat()
                    if (path.is_symlink() or not path.resolve().is_relative_to(root)
                            or (old.st_dev, old.st_ino, old.st_size, old.st_mtime_ns)
                            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)):
                        raise RuntimeError("清理期間檔案改變；停止此工作。")
                    path.unlink()
                    report["deleted_files"] += 1
                    report["deleted_bytes"] += old.st_size
                    audit["deleted_files"].append(str(path.relative_to(job)))
                audit["status"] = "cleaned"
                audit["completed_at"] = time.time()
                pipeline.write_json(job / AUDIT, audit)
            except (OSError, RuntimeError) as exc:
                report["errors"].append({"job_id": job.name, "error": str(exc)})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="執行刪除；未指定時只預覽")
    args = parser.parse_args()
    # Drive credentials live in the service .env, like the MCP server's.
    pipeline.load_dotenv(Path(__file__).resolve().parent / ".env")
    try:
        report = cleanup(args.jobs_root, apply=args.apply)
    except pipeline.WriterBusyError:
        print(json.dumps({"status": "busy", "deleted_files": 0}))
        return 0
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
