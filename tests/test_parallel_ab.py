"""A/B 並行（#57 §4.1–§4.2）：prepare 後自動同時跑分析與 Drive 上傳。

交接內容只放 Drive 檔案 ID／網址、分段時間、檔案大小，不含任何 A 的
逐字稿、畫面證據或摘要；小工具提供一鍵複製與開新一般對話入口。
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_queue import JobQueue


def _prepared():
    return {
        "status": "ready",
        "job_id": "0123456789ab",
        "title": "平行測試影片",
        "source_url": "https://example.test/video",
        "duration_seconds": 60.0,
        "segment_seconds": 30,
        "segments": [
            {"index": 0, "filename": "segment_000.mp4",
             "start_seconds": 0, "end_seconds": 30, "size_bytes": 8,
             "mime_type": "video/mp4", "url": "https://media.example/0"},
            {"index": 1, "filename": "segment_001.mp4",
             "start_seconds": 30, "end_seconds": 60, "size_bytes": 9,
             "mime_type": "video/mp4", "url": "https://media.example/1"},
        ],
    }


def _drive():
    return {
        "status": "drive_ready",
        "job_id": "0123456789ab",
        "folder_id": "folder-1",
        "folder_url": "https://drive.google.com/drive/folders/folder-1",
        "files": [
            {"file_id": "file-0", "url": "https://drive.google.com/file/d/file-0/view",
             "file_name": "segment_000.mp4", "mime_type": "video/mp4",
             "size_bytes": 8, "sha256": "a" * 64, "verified": True},
            {"file_id": "file-1", "url": "https://drive.google.com/file/d/file-1/view",
             "file_name": "segment_001.mp4", "mime_type": "video/mp4",
             "size_bytes": 9, "sha256": "b" * 64, "verified": True},
        ],
    }


class ParallelABTests(unittest.TestCase):
    def test_handoff_lists_drive_ids_urls_sizes_and_segment_times(self):
        from mcp_server import build_drive_handoff_text
        text = build_drive_handoff_text(_prepared(), _drive())
        for expected in ("file-0", "file-1",
                         "https://drive.google.com/file/d/file-0/view",
                         "8 bytes", "9 bytes",
                         "segment_000.mp4", "segment_001.mp4",
                         "0123456789ab"):
            self.assertIn(expected, text)

    def test_handoff_directs_independent_b_analysis_with_fixed_format(self):
        from mcp_server import build_drive_handoff_text
        text = build_drive_handoff_text(_prepared(), _drive())
        for expected in ("download_raw_file=true", "SHA-256",
                         "一句話重點", "分段表", "限制說明",
                         "合併", "不要讀取"):
            self.assertIn(expected, text)

    def test_handoff_contains_no_server_analysis_content(self):
        from mcp_server import build_drive_handoff_text
        text = build_drive_handoff_text(_prepared(), _drive())
        for forbidden in ("逐字稿", "畫面證據", "transcript", "timeline",
                          "visual_summary", " MiniMax 分析"):
            self.assertNotIn(forbidden, text)

    def test_drive_tool_result_carries_handoff_text(self):
        import mcp_server
        result = mcp_server.drive_tool_result(
            {**_drive(), "job_id": "0123456789ab"})
        self.assertEqual(result.structuredContent["status"], "drive_ready")
        self.assertIn("file-0", result.structuredContent["handoff_text"])
        self.assertIn("不要讀取", result.structuredContent["handoff_text"])
        mcp_server.DriveUploadResult.model_validate(result.structuredContent)

    def test_completed_drive_state_requires_verified_files(self):
        import mcp_server
        with tempfile.TemporaryDirectory() as raw:
            job_dir = Path(raw)
            manifest = {"job_id": "0123456789ab",
                        "source": {"title": "t", "url": "https://example.test/v"},
                        "segments": [{"filename": "segment_000.mp4"}]}
            state = {"status": "drive_ready", "job_id": "0123456789ab",
                     "folder_id": "folder-1", "folder_url": "https://drive.example/f",
                     "files": [{"file_id": "file-0",
                                "url": "https://drive.example/file-0",
                                "file_name": "segment_000.mp4",
                                "mime_type": "video/mp4",
                                "size_bytes": 8, "sha256": "a" * 64,
                                "verified": True}]}
            (job_dir / "drive-manifest.json").write_text(
                json.dumps(state), encoding="utf-8")
            valid = mcp_server.completed_drive_state(job_dir, manifest)
            self.assertIsNotNone(valid)
            self.assertEqual(valid["files"][0]["file_id"], "file-0")
            state["files"][0]["verified"] = False
            (job_dir / "drive-manifest.json").write_text(
                json.dumps(state), encoding="utf-8")
            self.assertIsNone(mcp_server.completed_drive_state(job_dir, manifest))

    def test_for_job_lists_pending_operations_for_widget_adoption(self):
        with tempfile.TemporaryDirectory() as raw:
            queue = JobQueue(Path(raw) / "queue", autostart=False)
            try:
                queue.submit("drive", {"job_id": "0123456789ab"}, "0123456789ab")
                queue.submit("analyze", {"job_id": "0123456789ab"}, "0123456789ab")
                snapshot = queue.for_job("0123456789ab")
            finally:
                queue.close()
            kinds = {item["operation"] for item in snapshot["pending_operations"]}
            self.assertEqual(kinds, {"drive", "analyze"})
            self.assertTrue(all(item["status"] in {"queued", "running"}
                                for item in snapshot["pending_operations"]))

    def test_get_prepared_video_reports_drive_config_and_state(self):
        import mcp_server
        from job_queue import JobQueue
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            jobs = root / "mcp_jobs"
            job = jobs / "0123456789ab"
            (job / "segments").mkdir(parents=True)
            (job / "source.mp4").write_bytes(b"original source")
            (job / "segments" / "segment_000.mp4").write_bytes(b"0123456789")
            manifest = {"version": 2, "status": "ready", "job_id": "0123456789ab",
                        "source": {"title": "fixture", "url": "https://example.test/video",
                                   "duration_seconds": 10},
                        "segment_seconds": 15, "max_segments": None,
                        "segments": [{"index": 0, "filename": "segment_000.mp4",
                                      "start_seconds": 0, "end_seconds": 10,
                                      "size_bytes": 10, "mime_type": "video/mp4"}]}
            (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            drive_state = {"status": "drive_ready", "job_id": "0123456789ab",
                           "folder_id": "folder-1",
                           "folder_url": "https://drive.example/f",
                           "files": [{"file_id": "file-0",
                                      "url": "https://drive.example/file-0",
                                      "file_name": "segment_000.mp4",
                                      "mime_type": "video/mp4",
                                      "size_bytes": 10, "sha256": "c" * 64,
                                      "verified": True}]}
            (job / "drive-manifest.json").write_text(
                json.dumps(drive_state), encoding="utf-8")
            queue = JobQueue(root / "queue", autostart=False)
            patches = [patch.object(mcp_server, "MCP_ROOT", root),
                       patch.object(mcp_server, "MCP_JOBS_ROOT", jobs),
                       patch.object(mcp_server, "MCP_MIN_FREE_BYTES", 0),
                       patch.dict(os.environ, {"GOOGLE_DRIVE_ACCESS_TOKEN": "",
                                               "GOOGLE_DRIVE_REFRESH_TOKEN": "",
                                               "GOOGLE_DRIVE_CLIENT_ID": "",
                                               "GOOGLE_DRIVE_CLIENT_SECRET": ""}),
                       patch.object(mcp_server, "get_queue", return_value=queue)]
            for item in patches:
                item.start()
            try:
                snapshot = asyncio.run(
                    mcp_server.get_prepared_video("0123456789ab"))
            finally:
                for item in reversed(patches):
                    item.stop()
                queue.close()
            self.assertFalse(snapshot["drive_configured"])
            self.assertEqual(snapshot["drive"]["files"][0]["file_id"], "file-0")
            self.assertIn("不要讀取", snapshot["drive"]["handoff_text"])
            self.assertEqual(snapshot["pending_operations"], [])


if __name__ == "__main__":
    unittest.main()
