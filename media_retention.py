"""Delete only expired, successfully analyzed MCP media; dry-run by default."""
from __future__ import annotations

import argparse
import collections
import json
import re
import stat
import time
from pathlib import Path

import pipeline


MEDIA = re.compile(
    r"(?:source\.(?:mp4|mkv|webm|mov|avi|m4v)|threads_media_\d+\.mp4|"
    r"segments/segment_\d+\.mp4|previews/preview_\d+\.jpg|"
    r"analysis/frames/frame_\d+\.jpg|analysis/audio/audio_\d+\.mp3)", re.I)
AUDIT = "media-retention.json"


def eligible_media(job: Path, cutoff: float) -> tuple[list[Path], str]:
    if job.is_symlink() or getattr(job, "is_junction", lambda: False)():
        return [], "linked_path"
    paths = list(job.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in [job, *paths]):
        return [], "linked_path"
    files = [p for p in paths if p.is_file()]
    if any(p.name in {".keep-media", "drive-manifest.json"} or p.suffix in {".tmp", ".part", ".ytdl"} for p in files):
        return [], "review_or_incomplete"
    try:
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        result_path = job / "analysis/result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (manifest["status"] != "ready" or manifest["job_id"] != job.name
                or result["status"] != "complete" or result["job_id"] != job.name
                or result.get("errors") or result["quality"]["status"] != "evidence_available"):
            return [], "not_complete_or_needs_review"
        source = pipeline.locate_source_video(job)
        if not source.is_file() or result["source_fingerprint"] != manifest["source"]["fingerprint"]:
            return [], "source_changed"
        if pipeline.file_fingerprint(source) != result["source_fingerprint"]:
            return [], "source_changed"
        # result.json is written only on completed analysis. Later writes extend retention.
        if max(p.stat().st_mtime for p in files if p.name != AUDIT) >= cutoff:
            return [], "younger_than_retention"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return [], "missing_or_invalid_evidence"
    selected = [p for p in files if MEDIA.fullmatch(p.relative_to(job).as_posix())]
    if any(not stat.S_ISREG(p.lstat().st_mode) or p.stat().st_nlink != 1 for p in selected):
        return [], "linked_or_nonregular_media"
    # Keep the source until everything else has been removed, so interrupted runs can resume.
    return sorted(selected, key=lambda p: (p == source, str(p))), "eligible"


def cleanup(jobs_root: Path, *, apply: bool = False, now: float | None = None) -> dict:
    root = jobs_root.absolute()
    if root.name != "mcp_jobs" or not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError("清理目標必須是明確、既有且無連結的 mcp_jobs 絕對路徑。")
    timestamp = time.time() if now is None else now
    report = {"mode": "apply" if apply else "dry_run", "retention_days": 7,
              "eligible_jobs": 0, "selected_files": 0, "selected_bytes": 0,
              "deleted_files": 0, "deleted_bytes": 0, "skipped": collections.Counter(), "errors": []}
    with pipeline.file_writer_lock(root / ".writer.lock"):
        for job in sorted(root.iterdir()):
            if not re.fullmatch(r"[a-f0-9]{12}", job.name) or not job.is_dir():
                continue
            try:
                paths, reason = eligible_media(job, timestamp - 7 * 86400)
                if not paths:
                    report["skipped"][reason] += 1
                    continue
                snapshots = {p: p.lstat() for p in paths}
                report["eligible_jobs"] += 1
                report["selected_files"] += len(paths)
                report["selected_bytes"] += sum(s.st_size for s in snapshots.values())
                if not apply:
                    continue
                audit = {"status": "deleting", "started_at": timestamp, "retention_days": 7,
                         "planned_files": [str(p.relative_to(job)) for p in paths], "deleted_files": []}
                pipeline.write_json(job / AUDIT, audit)
                for path in paths:
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
    try:
        report = cleanup(args.jobs_root, apply=args.apply)
    except pipeline.WriterBusyError:
        print(json.dumps({"status": "busy", "deleted_files": 0}))
        return 0
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
