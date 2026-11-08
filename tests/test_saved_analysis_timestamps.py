"""Historical analysis must not bypass current timestamp and retention guards."""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server as server
import media_retention
import pipeline


class SavedAnalysisTimestampTests(unittest.TestCase):
    def evidence(self, stamp=805.47):
        return {"status": "complete", "duration_seconds": 524.006,
                "audio_status": "skipped", "visual_status": "complete",
                "transcript": {"segments": []}, "errors": [],
                "quality": {"status": "evidence_available", "reasons": [], "warnings": [], "limitation": "old"},
                "timeline": [{"timestamp": stamp, "type": "visual", "data": {
                    "timestamp": stamp, "visual_summary": "Saved observation", "confidence": 0.9,
                    "analysis_method": "minimax_video"}}]}

    def test_poll_and_direct_result_recheck_without_mutating_saved_evidence(self):
        for stamp in (805.47, -1, None, "invalid", True, 40):
            with self.subTest(stamp=stamp):
                saved = self.evidence(stamp)
                before = copy.deepcopy(saved)
                payload = {"status": "completed", "result_kind": "analysis", "result": saved}
                with patch.object(server, "get_queue") as queue:
                    queue.return_value.get.return_value = payload
                    polled = asyncio.run(server.get_video_job("a" * 32))["result"]
                direct = server.analysis_tool_result(saved).structuredContent
                for result in (polled, direct):
                    row = result["timeline"][0]
                    self.assertIsNone(row["timestamp"])
                    self.assertIsNone(row["data"]["timestamp"])
                    self.assertEqual(row["data"]["timestamp_original"], stamp)
                    self.assertIs(row["data"]["timestamp_reliable"], False)
                    self.assertTrue(row["data"]["timestamp_issue"])
                    self.assertTrue(result["quality"]["warnings"])
                self.assertEqual(saved, before)
                self.assertEqual(payload["result"], before)

    def test_validated_video_time_remains_usable(self):
        result = self.evidence(40)
        result["timeline"][0]["data"]["timestamp_reliable"] = True
        refreshed = server.analysis_tool_result(result).structuredContent
        self.assertEqual(refreshed["timeline"][0]["timestamp"], 40)
        self.assertEqual(refreshed["quality"]["warnings"], [])
        self.assertEqual(refreshed["status"], "complete")

    def test_saved_frame_sampled_rows_are_dropped_and_reopened(self):
        for method in ("minimax_video_fallback_frames", "minimax", "local_uniformity_check", None):
            with self.subTest(method=method):
                result = self.evidence(40)
                data = result["timeline"][0]["data"]
                if method is None:
                    del data["analysis_method"]
                else:
                    data["analysis_method"] = method
                before = copy.deepcopy(result)
                payload = {"status": "completed", "result_kind": "analysis", "result": result}
                with patch.object(server, "get_queue") as queue:
                    queue.return_value.get.return_value = payload
                    polled = asyncio.run(server.get_video_job("a" * 32))["result"]
                direct = server.analysis_tool_result(result).structuredContent
                for refreshed in (polled, direct):
                    self.assertEqual(refreshed["timeline"], [])
                    self.assertEqual(refreshed["status"], "partial")
                    self.assertEqual(refreshed["visual_status"], "error")
                    self.assertTrue(any("抽幀" in error for error in refreshed["errors"]))
                    self.assertNotEqual(refreshed["quality"]["status"], "evidence_available")
                self.assertEqual(result, before)

    def test_retention_keeps_warning_or_unvalidated_timestamp_media(self):
        for reason in ("out_of_range", "legacy", "warning", "unreliable"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                job = Path(temporary) / "mcp_jobs" / ("a" * 12)
                (job / "analysis").mkdir(parents=True)
                source = job / "source.mp4"
                source.write_bytes(b"owned fixture")
                manifest = {"status": "ready", "job_id": job.name,
                            "source": {"fingerprint": pipeline.file_fingerprint(source)}}
                result = self.evidence(805.47 if reason == "out_of_range" else 40)
                result.update(job_id=job.name, source_fingerprint=manifest["source"]["fingerprint"],
                              source_sha256=pipeline.file_sha256(source))
                if reason == "warning":
                    result["timeline"][0]["data"]["timestamp_reliable"] = True
                    result["quality"]["warnings"] = ["requires source review"]
                if reason == "unreliable":
                    result["timeline"][0]["data"]["timestamp_reliable"] = False
                (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                (job / "analysis/result.json").write_text(json.dumps(result), encoding="utf-8")
                now = time.time()
                for path in job.rglob("*"):
                    os.utime(path, (now - 9 * 86400, now - 9 * 86400))
                # Update the fingerprint after intentionally aging the owned fixture.
                manifest["source"]["fingerprint"] = pipeline.file_fingerprint(source)
                result["source_fingerprint"] = manifest["source"]["fingerprint"]
                for path, value in ((job / "manifest.json", manifest), (job / "analysis/result.json", result)):
                    path.write_text(json.dumps(value), encoding="utf-8")
                    os.utime(path, (now - 9 * 86400, now - 9 * 86400))
                selected, skipped = media_retention.eligible_media(job, now - 7 * 86400)
                self.assertEqual(selected, [])
                self.assertEqual(skipped, "not_complete_or_needs_review")
                self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
