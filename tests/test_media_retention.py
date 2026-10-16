import json
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import media_retention as retention
import mcp_server as server
import pipeline


class RetentionTests(unittest.TestCase):
    def make_job(self, root, age=8):
        job = root / "0123456789ab"
        stamp = self.now - age * 86400
        for name in ["source.mp4", "threads_media_000.mp4", "segments/segment_000.mp4",
                     "previews/preview_000.jpg", "analysis/frames/frame_000001.jpg", "analysis/audio/audio_0000.mp3"]:
            path = job / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            os.utime(path, (stamp, stamp))
        fingerprint = pipeline.file_fingerprint(job / "source.mp4")
        pipeline.write_json(job / "manifest.json", {"status": "ready", "job_id": job.name, "source": {"fingerprint": fingerprint}})
        pipeline.write_json(job / "analysis/result.json", {"status": "complete", "job_id": job.name,
            "source_fingerprint": fingerprint, "source_sha256": pipeline.file_sha256(job / "source.mp4"),
            "quality": {"status": "evidence_available"}, "transcript": "keep"})
        for name in [".download-complete", "summary.md", "captions.srt", "submission.json", "user-upload.mp4"]:
            (job / name).write_text("keep", encoding="utf-8")
        for path in job.rglob("*"):
            if path.is_file():
                os.utime(path, (stamp, stamp))
        return job

    def setUp(self):
        self.now = time.time()

    def test_dry_run_then_delete_only_generated_media_and_keep_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "mcp_jobs"
            job = self.make_job(root)
            preserved = {p: p.read_bytes() for p in job.rglob("*") if p.is_file() and not retention.MEDIA.fullmatch(p.relative_to(job).as_posix())}
            report = retention.cleanup(root, now=self.now)
            self.assertEqual(report["selected_files"], 6)
            self.assertEqual(report["deleted_files"], 0)
            self.assertTrue((job / "source.mp4").exists())
            report = retention.cleanup(root, apply=True, now=self.now)
            self.assertEqual(report["deleted_files"], 6)
            self.assertEqual(report["errors"], [])
            self.assertEqual(json.loads((job / retention.AUDIT).read_text())["status"], "cleaned")
            for path, content in preserved.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertEqual(retention.cleanup(root, apply=True, now=self.now)["deleted_files"], 0)

    def test_ineligible_jobs_are_untouched(self):
        for reason in ["exactly_7_days", "recent_write", "partial", "needs_review", "drive", "manual_hold", "invalid", "source_changed", "unfinished", "frame_era"]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / "mcp_jobs"
                job = self.make_job(root, 7 if reason == "exactly_7_days" else 8)
                result = job / "analysis/result.json"
                if reason in {"partial", "needs_review"}:
                    data = json.loads(result.read_text())
                    if reason == "partial": data["status"] = "partial"
                    else: data["quality"]["status"] = "needs_review"
                    pipeline.write_json(result, data)
                    os.utime(result, (self.now - 8 * 86400,) * 2)
                elif reason == "recent_write":
                    (job / "recent.json").write_text("{}")
                elif reason == "drive":
                    (job / "drive-manifest.json").write_text("{}")
                elif reason == "manual_hold":
                    (job / ".keep-media").touch()
                elif reason == "invalid":
                    result.write_text("{")
                elif reason == "source_changed":
                    (job / "source.mp4").write_bytes(b"changed")
                elif reason == "unfinished":
                    (job / "source.mp4.part").touch()
                elif reason == "frame_era":
                    # A pre-removal frame-sampled result must keep the source for re-analysis.
                    data = json.loads(result.read_text())
                    data["timeline"] = [{"type": "visual", "timestamp": 1.0, "data": {
                        "visual_summary": "old frame", "confidence": 0.9, "analysis_method": "minimax"}}]
                    pipeline.write_json(result, data)
                    os.utime(result, (self.now - 8 * 86400,) * 2)
                self.assertEqual(retention.cleanup(root, apply=True, now=self.now)["deleted_files"], 0)
                self.assertTrue((job / "source.mp4").exists())

    def test_cross_process_lock_blocks_cleanup_and_mcp_then_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "mcp_jobs"
            self.make_job(root)
            with pipeline.file_writer_lock(root / ".writer.lock"):
                completed = subprocess.run([sys.executable, str(Path(retention.__file__)), "--jobs-root", str(root), "--apply"], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["status"], "busy")
                with patch.object(server, "MCP_JOBS_ROOT", root), self.assertRaises(server.JobBusyError):
                    server.run_job(lambda: self.fail("must not run"))
            with patch.object(server, "MCP_JOBS_ROOT", root):
                self.assertEqual(server.run_job(lambda: "ok"), "ok")

    @unittest.skipIf(os.name == "nt", "symlink creation needs Windows privileges")
    def test_symlink_and_hardlink_do_not_escape_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "mcp_jobs"
            job = self.make_job(root)
            outside = Path(directory) / "outside.mp4"
            outside.write_bytes(b"keep outside")
            (job / "analysis/frames/frame_999999.jpg").symlink_to(outside)
            self.assertEqual(retention.cleanup(root, apply=True, now=self.now)["deleted_files"], 0)
            self.assertEqual(outside.read_bytes(), b"keep outside")
            (job / "analysis/frames/frame_999999.jpg").unlink()
            os.link(job / "source.mp4", Path(directory) / "source-hardlink.mp4")
            self.assertEqual(retention.cleanup(root, apply=True, now=self.now)["deleted_files"], 0)

    def test_expired_source_can_be_downloaded_again_without_removing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".download-complete").touch()
            def downloaded(*args, **kwargs):
                (root / "source.mp4").write_bytes(b"new")
                return root / "source.mp4"
            with patch.object(pipeline, "download_threads_video", side_effect=downloaded) as fetch:
                self.assertEqual(pipeline.download_video("https://threads.com/t/ABC", root), root / "source.mp4")
                fetch.assert_called_once()

    def test_queue_and_lifecycle_protection_and_new_run_media(self):
        for state in ["queued", "running", "failed", "cancelled", "interrupted", "expired", "completed", "live_share", "changed_bytes"]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / "mcp_jobs"
                job = self.make_job(root)
                frame = job / "analysis/runs" / ("a" * 64) / "budgeted-frames/frame_000001.jpg"
                frame.parent.mkdir(parents=True)
                frame.write_bytes(b"frame")
                os.utime(frame, (self.now - 8 * 86400,) * 2)
                state_dir = root.parent / ".video-state"
                state_dir.mkdir()
                db_path = state_dir / "jobs.sqlite3"
                with closing(sqlite3.connect(db_path)) as db, db:
                    db.execute("CREATE TABLE jobs (job_id TEXT,status TEXT)")
                    db.execute("INSERT INTO jobs VALUES (?,?)", (job.name, state if state not in {"live_share", "changed_bytes"} else "completed"))
                if state == "live_share":
                    marker = job / ".media-lifecycle.json"
                    pipeline.write_json(marker, {"version": 1, "job_id": job.name,
                        "managed_at": self.now - 8 * 86400, "last_used_at": self.now - 8 * 86400,
                        "protected_until": self.now + 100})
                    os.utime(marker, (self.now - 8 * 86400,) * 2)
                if state == "changed_bytes":
                    source = job / "source.mp4"
                    stamp = source.stat()
                    source.write_bytes(b"other")
                    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                report = retention.cleanup(root, apply=True, now=self.now)
                self.assertEqual(report["deleted_files"], 7 if state == "completed" else 0)
                self.assertTrue((job / "analysis/result.json").exists())
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_real_worker_dispatch_respects_cleanup_lock_for_every_operation(self):
        import job_worker
        for operation, function in [("prepare", "prepare_video_sync"), ("analyze", "analyze_prepared_sync"), ("drive", "upload_prepared_drive_sync")]:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                work = Path(directory).resolve()
                jobs = work / "mcp_jobs"
                jobs.mkdir()
                pipeline.write_json(work / "input.json", {"operation": operation, "inputs": {}})
                with (patch.object(job_worker.sys, "argv", ["job_worker.py", str(work)]),
                      patch.object(job_worker.threading.Thread, "start"),
                      patch.object(job_worker, "report_progress"),
                      patch.object(server, "MCP_JOBS_ROOT", jobs),
                      patch.object(server, function) as provider,
                      pipeline.file_writer_lock(jobs / ".writer.lock")):
                    job_worker.main()
                    provider.assert_not_called()
                self.assertFalse(json.loads((work / "output.json").read_text())["ok"])


if __name__ == "__main__":
    unittest.main()
