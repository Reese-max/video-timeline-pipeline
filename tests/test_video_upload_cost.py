"""Upload budgets/receipts use fixture bytes and mocked provider transport only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import cost_tracker
import pipeline


class UploadCostTests(unittest.TestCase):
    def test_hard_stop_refuses_before_request_and_multipart(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            video = root / "fixture.mp4"
            video.write_bytes(b"synthetic fixture")
            with cost_tracker.cost_root_scope(root), patch.dict(os.environ, {
                "MINIMAX_BASE_URL": "https://api.minimax.io/v1", "BUDGET_HARD_STOP": "true",
                "DAILY_BUDGET_USD": "0.000001"}), patch.object(pipeline, "request_json") as request, \
                patch.object(pipeline, "multipart_body") as multipart:
                with self.assertRaises(cost_tracker.BudgetBlockedError):
                    pipeline.minimax_upload_video_understanding(video, "fake-key")
                request.assert_not_called()
                multipart.assert_not_called()

    def test_success_and_retry_preserve_file_id_and_unknown_cost_per_attempt(self):
        for retry in (False, True):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                video = root / "fixture.mp4"
                video.write_bytes(b"synthetic fixture")
                response = contextlib.closing(io.BytesIO(json.dumps({"file": {"file_id": "fixture-id"}}).encode()))
                replies = [response]
                if retry:
                    replies.insert(0, urllib.error.HTTPError("https://api.minimax.io/v1/files/upload", 500,
                                   "fixture failure", {}, io.BytesIO(b'{"error":"temporary"}')))
                with cost_tracker.cost_root_scope(root), patch.dict(os.environ, {
                    "MINIMAX_BASE_URL": "https://api.minimax.io/v1", "BUDGET_HARD_STOP": "false",
                    "DAILY_BUDGET_USD": "100"}), patch.object(pipeline.urllib.request, "urlopen", side_effect=replies) as request, \
                    patch.object(pipeline.time, "sleep", return_value=None):
                    self.assertEqual(pipeline.minimax_upload_video_understanding(video, "fake-key"), "fixture-id")
                with contextlib.closing(sqlite3.connect(root / "operations.db")) as db:
                    db.row_factory = sqlite3.Row
                    usage = db.execute("SELECT * FROM usage_costs ORDER BY rowid").fetchall()
                    reservations = db.execute("SELECT * FROM budget_reservations ORDER BY rowid").fetchall()
                self.assertEqual(request.call_count, 2 if retry else 1)
                self.assertEqual([row["attempt_no"] for row in usage], [1, 2] if retry else [1])
                self.assertEqual([row["status"] for row in usage], ["failed", "succeeded"] if retry else ["succeeded"])
                self.assertEqual({row["logical_operation_id"] for row in usage}, {usage[0]["logical_operation_id"]})
                self.assertEqual(usage[-1]["provider_request_id"], "fixture-id")
                self.assertTrue(all(row["provider"] == "minimax" and row["operation"] == "files.upload" for row in usage))
                self.assertTrue(all(row["request_units"] == video.stat().st_size for row in usage))
                self.assertTrue(all(row["cost_status"] == "unknown" and row["actual_micro_usd"] is None for row in usage))
                self.assertTrue(all(row["estimated_micro_usd"] == 0 for row in usage))
                self.assertEqual({row["reservation_id"] for row in usage}, {row["reservation_id"] for row in reservations})
                self.assertTrue(all(row["status"] == "settled" for row in reservations))


if __name__ == "__main__":
    unittest.main()
