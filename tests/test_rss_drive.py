"""RSS results reach Drive and the completion notice: one folder, no overwrites, no failed videos."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import drive_storage
import email_providers
import notifications
import pipeline

KNOWLEDGE = "# 影片知識檔\n\n重點：測試內容。\n".encode("utf-8")
ENV = {"GOOGLE_DRIVE_ACCESS_TOKEN": "token", "GOOGLE_DRIVE_FOLDER_ID": "parent_1"}


def make_job(root: Path, data: bytes = KNOWLEDGE) -> Path:
    job_dir = root / "jobs" / "job0123456789"
    job_dir.mkdir(parents=True)
    (job_dir / "knowledge.md").write_bytes(data)
    return job_dir


class FakeDrive:
    """Just enough Drive: files keyed by pre-allocated ID."""

    def __init__(self, ids: list[str]):
        self.ids = list(ids)
        self.remote: dict[str, dict] = {}
        self.uploads: list[str] = []
        self.state_seen_before_upload: list[bool] = []

    def new_id(self, token: str) -> str:
        return self.ids.pop(0)

    def get_file(self, token: str, file_id: str):
        return self.remote.get(file_id)

    def create_folder(self, token: str, name: str, parent_id: str, file_id: str | None = None):
        self.remote[file_id] = {"id": file_id, "name": name, "mimeType": drive_storage.FOLDER_MIME,
                                "parents": [parent_id], "trashed": False}
        return self.remote[file_id]

    def upload(self, job_dir: Path):
        def upload_file(token, path, name, parent_id, mime_type="video/mp4", *, file_id=None):
            saved = [json.loads(p.read_text(encoding="utf-8")) for p in job_dir.glob("drive-knowledge-*.json")]
            self.state_seen_before_upload.append(any(s.get("file_id") == file_id for s in saved))
            data = path.read_bytes()
            self.uploads.append(file_id)
            self.remote[file_id] = {"id": file_id, "name": name, "mimeType": mime_type, "size": str(len(data)),
                                    "sha256Checksum": hashlib.sha256(data).hexdigest(), "parents": [parent_id],
                                    "trashed": False, "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
            return self.remote[file_id]
        return upload_file

    def patches(self, job_dir: Path):
        return (patch.dict(drive_storage.os.environ, ENV),
                patch.object(drive_storage, "_access_token", return_value="token"),
                patch.object(drive_storage, "_new_id", side_effect=self.new_id),
                patch.object(drive_storage, "_get_file", side_effect=self.get_file),
                patch.object(drive_storage, "_create_folder", side_effect=self.create_folder),
                patch.object(drive_storage, "_upload_file", side_effect=self.upload(job_dir)))


def run_with(drive: FakeDrive, job_dir: Path, root: Path, title: str = "測試影片"):
    p = drive.patches(job_dir)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        return drive_storage.upload_rss_knowledge(root, job_dir, title)


class UploadRssKnowledgeTests(unittest.TestCase):
    def test_first_upload_reserves_ids_before_writing_and_verifies_the_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            drive = FakeDrive(["rssfolder_1", "file_1"])
            state = run_with(drive, job_dir, root)
            self.assertEqual(drive.uploads, ["file_1"])
            self.assertEqual(drive.state_seen_before_upload, [True], "file ID must be durable before the upload")
            self.assertTrue(state["verified"])
            self.assertEqual(state["url"], "https://drive.google.com/file/d/file_1/view")
            self.assertEqual(drive.remote["file_1"]["parents"], ["rssfolder_1"])
            self.assertEqual(drive.remote["file_1"]["mimeType"], "text/markdown")
            self.assertIn("測試影片", drive.remote["file_1"]["name"])
            folder = json.loads((root / "rss" / "drive-folder.json").read_text(encoding="utf-8"))
            self.assertEqual((folder["folder_id"], folder["parent_id"]), ("rssfolder_1", "parent_1"))

    def test_rerun_with_the_same_bytes_reuses_the_verified_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            drive = FakeDrive(["rssfolder_1", "file_1"])
            run_with(drive, job_dir, root)
            again = run_with(drive, job_dir, root)
            self.assertEqual(drive.uploads, ["file_1"])
            self.assertEqual(again["file_id"], "file_1")

    def test_same_bytes_with_a_corrected_title_reuse_the_first_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            drive = FakeDrive(["rssfolder_1", "file_1"])
            run_with(drive, job_dir, root, title="舊標題")
            again = run_with(drive, job_dir, root, title="新標題")
            self.assertEqual(drive.uploads, ["file_1"])
            self.assertIn("舊標題", again["file_name"])

    def test_changed_local_bytes_get_a_new_file_and_leave_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            drive = FakeDrive(["rssfolder_1", "file_1", "file_2"])
            run_with(drive, job_dir, root)
            old = dict(drive.remote["file_1"])
            (job_dir / "knowledge.md").write_bytes(KNOWLEDGE + "補充。\n".encode("utf-8"))
            second = run_with(drive, job_dir, root)
            self.assertEqual(drive.uploads, ["file_1", "file_2"])
            self.assertEqual(second["file_id"], "file_2")
            self.assertEqual(drive.remote["file_1"], old)

    def test_a_changed_drive_copy_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            drive = FakeDrive(["rssfolder_1", "file_1"])
            run_with(drive, job_dir, root)
            drive.remote["file_1"]["sha256Checksum"] = "0" * 64
            with self.assertRaises(ValueError):
                run_with(drive, job_dir, root)
            self.assertEqual(drive.uploads, ["file_1"])

    def test_a_trashed_rss_folder_blocks_every_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            drive = FakeDrive(["rssfolder_1", "file_1"])
            run_with(drive, job_dir, root)
            drive.remote["rssfolder_1"]["trashed"] = True
            (job_dir / "knowledge.md").write_bytes(KNOWLEDGE + b"more\n")
            with self.assertRaises(ValueError):
                run_with(drive, job_dir, root)
            self.assertEqual(drive.uploads, ["file_1"])

    def test_missing_credentials_raise_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            empty = {k: "" for k in ("GOOGLE_DRIVE_ACCESS_TOKEN", "GOOGLE_DRIVE_REFRESH_TOKEN",
                                     "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET")}
            with patch.dict(drive_storage.os.environ, empty):
                with self.assertRaises(drive_storage.DriveNotConfigured):
                    drive_storage.upload_rss_knowledge(root, job_dir, "x")


class PipelineRssResultTests(unittest.TestCase):
    def test_upload_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(pipeline.os.environ, {**ENV, "RSS_DRIVE_UPLOAD": "false"}), \
             patch.object(drive_storage, "upload_rss_knowledge") as upload:
            root = Path(tmp)
            self.assertEqual(pipeline.upload_rss_result(root, make_job(root), {})["status"], "disabled")
            upload.assert_not_called()

    def test_without_drive_credentials_nothing_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive_storage, "configured", return_value=False), \
             patch.object(drive_storage, "upload_rss_knowledge") as upload:
            root = Path(tmp)
            self.assertEqual(pipeline.upload_rss_result(root, make_job(root), {})["status"], "not_configured")
            upload.assert_not_called()

    def test_a_drive_failure_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive_storage, "configured", return_value=True), \
             patch.object(drive_storage, "upload_rss_knowledge", side_effect=RuntimeError("Google Drive 上傳失敗（HTTP 503）")):
            root = Path(tmp)
            result = pipeline.upload_rss_result(root, make_job(root), {"source": {"title": "t"}})
            self.assertEqual(result["status"], "error")
            self.assertIn("503", result["error"])

    def test_success_keeps_only_the_file_id_and_link(self):
        state = {"file_id": "file_1", "url": "https://drive.google.com/file/d/file_1/view", "verified": True, "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive_storage, "configured", return_value=True), \
             patch.object(drive_storage, "upload_rss_knowledge", return_value=state) as upload:
            root = Path(tmp)
            job_dir = make_job(root)
            result = pipeline.upload_rss_result(root, job_dir, {"source": {"title": "標題"}})
            self.assertEqual(result, {"status": "uploaded", "file_id": "file_1", "url": state["url"]})
            upload.assert_called_once_with(root, job_dir, "標題")

    def test_a_manifest_write_failure_does_not_fail_the_completed_video(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive_storage, "configured", return_value=False), \
             patch.object(pipeline, "write_json", side_effect=OSError("No space left on device")):
            root = Path(tmp)
            job_dir = make_job(root)
            manifest = {"status": "completed", "source": {"title": "t"}}
            pipeline.attach_rss_drive(root, job_dir, job_dir / "manifest.json", manifest)
            self.assertEqual(manifest["drive"], {"status": "not_configured"})


class CompletionNoticeTests(unittest.TestCase):
    def test_notice_carries_the_short_summary_and_drive_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            (job_dir / "summary.json").write_text(json.dumps({"short_summary": "這支影片在講測試。" * 40}, ensure_ascii=False), encoding="utf-8")
            manifest = {"source": {"title": "RSS 影片", "url": "https://www.youtube.com/watch?v=abc"},
                        "drive": {"status": "uploaded", "file_id": "file_1", "url": "https://drive.google.com/file/d/file_1/view"}}
            pipeline.write_json(job_dir / "transcript.json", {"segments": [{"start": 0, "end": 1, "text": "fixture"}]})
            pipeline.write_json(job_dir / "timeline.json", [{"timestamp": 0, "end": 1, "type": "speech", "text": "fixture"}])
            manifest.update(status="completed", artifacts={key: str((job_dir / f"{key}.json").relative_to(root))
                                                           for key in ("transcript", "timeline", "summary")})
            fake = email_providers.FakeProvider(mode="accepted")
            with patch("notifications.get_configured_provider", return_value=fake), \
                 patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x"}):
                notifications.record_pipeline_completed_event(root, job_dir, manifest)
            body = fake.history[0]["text_body"]
            self.assertIn("這支影片在講測試。", body)
            self.assertIn("https://drive.google.com/file/d/file_1/view", body)
            self.assertLess(len(body), 1000, "the notice is a pointer, not the whole summary")
            self.assertIn("drive.google.com/file/d/file_1/view", fake.history[0]["html_body"])

    def test_html_in_a_feed_title_is_escaped(self):
        _, html_body = notifications.render_event_email("video.completed", {"title": "<img src=x onerror=alert(1)>", "url": ""})
        self.assertNotIn("<img", html_body)
        self.assertIn("&lt;img", html_body)

    def test_incomplete_job_without_summary_cannot_emit_completion_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = make_job(root)
            fake = email_providers.FakeProvider(mode="accepted")
            with patch("notifications.get_configured_provider", return_value=fake), \
                 patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x"}):
                result = notifications.record_pipeline_completed_event(root, job_dir, {"source": {"title": "舊影片", "url": ""}})
            self.assertIsNone(result)
            self.assertEqual(fake.history, [])
            self.assertFalse((root / "operations.db").exists())


if __name__ == "__main__":
    unittest.main()
