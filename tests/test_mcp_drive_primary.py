"""MCP side of Drive-primary media: restore on demand and always queue the Drive backup."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import drive_storage
import mcp_server as server
import pipeline
from test_drive_backup_restore import JOB, build_job


class DrivePrimaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "mcp_jobs"
        self.job, self.manifest, self.state = build_job(self.root, now=10**9)
        self.saved = {name: (self.job / name).read_bytes()
                      for name in ("source.mp4", "segments/segment_000.mp4")}
        for name in ("source.mp4", "segments/segment_000.mp4", "previews/preview_000.jpg"):
            (self.job / name).unlink()
        for patcher in (patch.object(server, "MCP_JOBS_ROOT", self.root),
                        patch.object(server, "report_progress"),
                        patch.object(server, "check_job_capacity"),
                        patch.object(server, "require_disk_space")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_restore(self, job_dir, state):
        for name, content in self.saved.items():
            (job_dir / name).write_bytes(content)
        source = state["source_file"]
        os.utime(job_dir / "source.mp4", ns=(source["mtime_ns"], source["mtime_ns"]))
        return list(self.saved)

    def fake_preview(self, segment, preview):
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"jpg")
        return True

    def test_cleaned_media_is_restored_from_drive_with_previews(self):
        self.assertIsNone(server.prepared_media_path(self.job, self.manifest))
        with patch.object(drive_storage, "restore_prepared_media", side_effect=self.fake_restore) as restore, \
             patch.object(server, "extract_preview", side_effect=self.fake_preview):
            path = server.ensure_local_media(self.job, self.manifest)
        restore.assert_called_once()
        self.assertEqual(path, self.job / "source.mp4")
        self.assertEqual(pipeline.file_fingerprint(path), self.manifest["source"]["fingerprint"])

    def test_a_newer_local_source_is_never_replaced_by_the_backup(self):
        (self.job / "source.mp4").write_bytes(b"newer source from the URL")
        with patch.object(drive_storage, "restore_prepared_media") as restore:
            self.assertIsNone(server.ensure_local_media(self.job, self.manifest))
        restore.assert_not_called()
        self.assertEqual((self.job / "source.mp4").read_bytes(), b"newer source from the URL")

    def test_drive_job_refuses_a_backup_drive_no_longer_holds(self):
        with patch.object(drive_storage, "verify_remote_backup", return_value=False), \
             patch.object(drive_storage, "upload_prepared_segments") as upload:
            with self.assertRaises(RuntimeError):
                server.upload_prepared_drive_sync(JOB)
        upload.assert_not_called()

    def test_without_complete_backup_nothing_is_downloaded(self):
        legacy = {k: v for k, v in self.state.items() if k != "source_file"}
        pipeline.write_json(self.job / "drive-manifest.json", legacy)
        with patch.object(drive_storage, "restore_prepared_media") as restore:
            self.assertIsNone(server.ensure_local_media(self.job, self.manifest))
        restore.assert_not_called()

    def test_prepare_restores_from_drive_instead_of_downloading_again(self):
        with patch.object(server, "validate_source_url", side_effect=lambda url: url), \
             patch.object(server.pipeline, "job_key", return_value=JOB), \
             patch.object(server, "storage_policy", return_value=MagicMock()), \
             patch.object(server, "check_job_capacity"), \
             patch.object(server.pipeline, "download_video", side_effect=AssertionError("re-download")), \
             patch.object(server.pipeline, "probe_video", return_value={
                 "format": {"duration": "60"}, "streams": [{"codec_type": "video"}]}), \
             patch.object(drive_storage, "restore_prepared_media", side_effect=self.fake_restore), \
             patch.object(server, "extract_preview", side_effect=self.fake_preview), \
             patch.object(server, "split_video", side_effect=AssertionError("re-split")):
            manifest = server.prepare_video_sync("https://example.com/video", 120, None)
        self.assertEqual(manifest, self.manifest)

    def test_drive_job_for_cleaned_media_reports_the_existing_backup(self):
        with patch.object(drive_storage, "verify_remote_backup", return_value=True), \
             patch.object(drive_storage, "upload_prepared_segments") as upload:
            result = server.upload_prepared_drive_sync(JOB)
        upload.assert_not_called()
        self.assertEqual(result["source_file"]["file_id"], "src_1")
        self.assertEqual(result["status"], "drive_ready")

    def test_expired_media_message_mentions_drive_restore(self):
        queue = MagicMock()
        queue.for_job.return_value = {"latest_operation": None}
        with patch.object(server, "get_queue", return_value=queue):
            result = asyncio.run(server.get_prepared_video(JOB))
        self.assertEqual(result["error_code"], "MEDIA_EXPIRED")
        self.assertTrue(result["drive_backup"])
        self.assertIn("Google Drive", result["error"])

    def test_analysis_queues_the_drive_backup_too(self):
        shutil.rmtree(self.job / "analysis")
        queue = MagicMock()
        queue.submit.side_effect = lambda operation, inputs, job_id: {
            "operation_id": "a" * 32, "operation": operation, "status": "queued", "job_id": job_id}
        with patch.object(server, "get_queue", return_value=queue), \
             patch.object(server, "storage_policy", return_value=MagicMock()), \
             patch.object(drive_storage, "configured", return_value=True):
            asyncio.run(server.analyze_prepared_video(JOB, background=True))
        self.assertEqual([call.args[0] for call in queue.submit.call_args_list], ["analyze", "drive"])

    def test_playback_links_record_their_expiry(self):
        policy = MagicMock()
        with patch.object(server, "storage_policy", return_value=policy), \
             patch.object(server, "media_signer", return_value=MagicMock(sign=MagicMock(return_value="sig"))):
            server.media_url(JOB, "segments/segment_000.mp4")
        self.assertTrue(policy.protect_until.call_args.kwargs.get("link"))


if __name__ == "__main__":
    unittest.main()
