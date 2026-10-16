"""Per-event source-time reliability after the existing MP4 coverage gate."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class VideoTimestampReliabilityTests(unittest.TestCase):
    @staticmethod
    def event(stamp):
        return {"timestamp": stamp, "screen_type": "影片片段", "visible_text": [],
                "visual_summary": "測試畫面", "important_details": [], "confidence": 0.9}

    def run_response(self, stamps, duration):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"fake-mp4")
            response = json.dumps({"events": [self.event(stamp) for stamp in stamps]})
            with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="file"), \
                 mock.patch.object(pipeline, "minimax_chat", return_value=response) as chat:
                observations = pipeline.analyze_video_mp4(
                    video, "fake-key", "MiniMax-M3", duration=duration, work_dir=root / "work")
            self.assertEqual(chat.call_count, 1)
            return observations

    def test_compressed_front_with_valid_outlier_marks_only_front_unreliable(self):
        stamps = [index * 6.9 / 16 for index in range(17)] + [90]
        observations = self.run_response(stamps, 166)
        self.assertEqual([row["timestamp"] for row in observations], stamps)
        self.assertTrue(all(row["timestamp_reliable"] is False for row in observations[:-1]))
        self.assertTrue(all("壓縮" in row["timestamp_issue"] for row in observations[:-1]))
        self.assertIs(observations[-1]["timestamp_reliable"], True)
        self.assertIsNone(observations[-1]["timestamp_issue"])

    def test_out_of_range_clamps_both_ends_and_preserves_original(self):
        observations = self.run_response([-3, -0.00001, "-2.5", "02:50", 90], 166)
        self.assertEqual([row["timestamp"] for row in observations], [0, 0, 0, 166, 90])
        self.assertEqual([row["timestamp_original"] for row in observations], [-3, -0.00001, "-2.5", "02:50", 90])
        for row in observations[:4]:
            self.assertIs(row["timestamp_reliable"], False)
            self.assertIn("超出", row["timestamp_issue"])
            self.assertIn(str(row["timestamp_original"]), row["timestamp_issue"])
        self.assertIs(observations[-1]["timestamp_reliable"], True)

    def test_rounding_up_to_one_second_clamps_without_losing_reliability(self):
        stamps = ["2:47", 167.6, 167.60001, -0.1]
        observations = self.run_response(stamps, 166.6)
        self.assertEqual([row["timestamp"] for row in observations], [166.6, 166.6, 166.6, 0])
        self.assertEqual([row["timestamp_original"] for row in observations], stamps)
        for row in observations[:2]:
            self.assertIs(row["timestamp_reliable"], True)
            self.assertIsNone(row["timestamp_issue"])
        for row in observations[2:]:
            self.assertIs(row["timestamp_reliable"], False)
            self.assertIn("超出", row["timestamp_issue"])

    def test_stamp_summary_and_incomplete_default_issues(self):
        for method in ("minimax_video", "minimax_video_compressed"):
            with self.subTest(method=method):
                rows = [{"screen_type": "整片摘要"},
                        {"screen_type": "影片", "completion_status": "incomplete"},
                        {"timestamp_original": "1:30", "timestamp_reliable": True, "timestamp_issue": None}]
                pipeline._stamp_video_observations(rows, analysis_method=method, source_video=Path("clip.mp4"))
                self.assertEqual([row["timestamp_issue"] for row in rows],
                                 ["整片摘要沒有可核對的時間碼", "畫面分析未完成，無法驗證時間碼", None])
                self.assertEqual([row["timestamp_reliable"] for row in rows], [False, False, True])
                self.assertEqual([row["timestamp_original"] for row in rows], [None, None, "1:30"])

    def test_concentration_starts_at_exactly_sixty_seconds(self):
        observations = self.run_response([1, 2, 3, 6, 30], 60)
        self.assertTrue(all(row["timestamp_reliable"] is False for row in observations[:4]))
        self.assertTrue(all("壓縮" in row["timestamp_issue"] for row in observations[:4]))
        self.assertIs(observations[-1]["timestamp_reliable"], True)

    def test_zero_and_negative_duration_are_unknown(self):
        for duration in (0, -1):
            with self.subTest(duration=duration):
                observations = self.run_response([1, "01:30"], duration)
                self.assertEqual([row["timestamp"] for row in observations], [1, 90])
                for row in observations:
                    self.assertIs(row["timestamp_reliable"], False)
                    self.assertEqual(row["timestamp_issue"], "缺少影片長度，無法驗證時間碼")

    def test_unknown_duration_marks_every_event_unreliable(self):
        observations = self.run_response([1, "01:30"], None)
        self.assertEqual([row["timestamp"] for row in observations], [1, 90])
        for row in observations:
            self.assertIs(row["timestamp_reliable"], False)
            self.assertEqual(row["timestamp_issue"], "缺少影片長度，無法驗證時間碼")

    def test_unknown_duration_still_clamps_negative_time_to_zero(self):
        observations = self.run_response([-3], None)
        self.assertEqual(observations[0]["timestamp"], 0)
        self.assertEqual(observations[0]["timestamp_original"], -3)
        self.assertIs(observations[0]["timestamp_reliable"], False)
        self.assertEqual(observations[0]["timestamp_issue"], "缺少影片長度，無法驗證時間碼")

    def test_normal_distribution_changes_only_reliability_fields(self):
        stamps = [0, 16.6, 50, 90, "02:46"]
        observations = self.run_response(stamps, 166)
        for stamp, row in zip(stamps, observations):
            self.assertEqual(row["timestamp_original"], stamp)
            self.assertIs(row["timestamp_reliable"], True)
            self.assertIsNone(row["timestamp_issue"])
            expected = {**self.event(pipeline.parse_video_timestamp(stamp)),
                        "visual_schema_version": pipeline.VISUAL_OBSERVATION_SCHEMA_VERSION,
                        "completion_status": "complete", "analysis_method": "minimax_video",
                        "source_video": row["source_video"], "minimax_file_id": "file"}
            self.assertEqual({key: value for key, value in row.items()
                              if key not in {"timestamp_original", "timestamp_reliable", "timestamp_issue"}}, expected)

    def test_concentration_boundary_preserves_specific_range_issue(self):
        observations = self.run_response([-1, 1, 2, 16.6, 90], 166)
        self.assertTrue(all(row["timestamp_reliable"] is False for row in observations[:4]))
        self.assertIn("超出", observations[0]["timestamp_issue"])
        self.assertNotIn("壓縮", observations[0]["timestamp_issue"])
        self.assertIn("壓縮", observations[3]["timestamp_issue"])
        self.assertIs(observations[4]["timestamp_reliable"], True)

    def test_concentration_requires_duration_count_and_eighty_percent(self):
        for stamps, duration in [([1, 2, 3, 4, 30], 59), ([1, 2, 3, 90], 166),
                                 ([1, 2, 3, 50, 90], 166)]:
            with self.subTest(stamps=stamps, duration=duration):
                self.assertTrue(all(row["timestamp_reliable"] is True
                                    for row in self.run_response(stamps, duration)))

    def test_legacy_mp4_cache_is_rejected_but_validated_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"fake-mp4")
            legacy = {**self.event(90), "analysis_method": "minimax_video"}
            with mock.patch.object(pipeline, "reuse_artifact", return_value={"observations": [legacy]}), \
                 mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="file") as upload, \
                 mock.patch.object(pipeline, "minimax_chat", return_value=json.dumps({"events": [self.event(90)]})):
                fresh = pipeline.analyze_video_mp4(video, "key", "MiniMax-M3", duration=166,
                                                  cache_dir=root / "cache", work_dir=root / "work")
            upload.assert_called_once()
            with mock.patch.object(pipeline, "reuse_artifact", return_value={"observations": fresh}), \
                 mock.patch.object(pipeline, "minimax_upload_video_understanding") as upload:
                cached = pipeline.analyze_video_mp4(video, "key", "MiniMax-M3", duration=166,
                                                   cache_dir=root / "cache", work_dir=root / "work")
            upload.assert_not_called()
            self.assertEqual(cached, fresh)

    def test_aggregate_mp4_cache_requires_validation_and_rejects_frames(self):
        legacy = {**self.event(90), "analysis_method": "minimax_video"}
        self.assertFalse(pipeline.reusable_video_artifact([legacy]))
        self.assertFalse(pipeline.reusable_video_artifact([{**legacy, "analysis_method": "minimax_video_compressed"}]))
        validated = {**legacy, "timestamp_original": 90, "timestamp_reliable": False,
                     "timestamp_issue": "缺少影片長度，無法驗證時間碼"}
        self.assertTrue(pipeline.reusable_video_artifact([validated]))
        self.assertFalse(pipeline.reusable_video_artifact([validated, legacy]))
        self.assertFalse(pipeline.reusable_video_artifact([{**validated, "completion_status": "incomplete"}]))
        self.assertFalse(pipeline.reusable_video_artifact([{**legacy, "analysis_method": "minimax_video_fallback_frames"}]))

    def test_markdown_unreliable_visual_time_has_no_jump_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = [{**self.event(2), "timestamp_reliable": False}, self.event(90)]
            path = pipeline.render_knowledge_markdown(
                root, {"title": "測試", "url": "https://www.youtube.com/watch?v=fixture"}, root / "clip.mp4",
                {"segments": []}, observations, pipeline.merge_timeline({"segments": []}, observations), None)
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "畫面：" in line]
            self.assertIn("時間碼不可靠", lines[0])
            self.assertNotIn("](https://", lines[0])
            self.assertIn("](https://", lines[1])


if __name__ == "__main__":
    unittest.main()
