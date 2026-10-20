"""Whole-video answers that skip a stretch get one targeted second look at the same upload."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def event(stamp, summary="測試畫面"):
    return {"timestamp": stamp, "screen_type": "影片片段", "visible_text": [],
            "visual_summary": summary, "important_details": [], "confidence": 0.9}


def answer(*stamps):
    return json.dumps({"events": [event(stamp) for stamp in stamps], "overall_summary": "摘要"})


class VideoEventGapTests(unittest.TestCase):
    def test_gaps_cover_start_middle_and_end(self):
        rows = [{"timestamp": stamp} for stamp in (40, 50, 125, 130)]
        self.assertEqual(pipeline.video_event_gaps(rows, 200), [(0.0, 40.0), (50.0, 125.0), (130.0, 200.0)])

    def test_thirty_seconds_is_allowed_and_out_of_range_stamps_are_clamped(self):
        rows = [{"timestamp": stamp} for stamp in (-5, 30, 60, 95)]
        self.assertEqual(pipeline.video_event_gaps(rows, 90), [])

    def test_unknown_duration_has_no_gaps(self):
        for duration in (None, 0, -1, float("nan")):
            with self.subTest(duration=duration):
                self.assertEqual(pipeline.video_event_gaps([{"timestamp": 0}], duration), [])


class CliVisualReuseTests(unittest.TestCase):
    def test_visual_json_from_before_gap_filling_is_not_reused(self):
        self.assertFalse(pipeline.gap_checked_video_config({"visual_mode": "video_mp4"}))
        self.assertFalse(pipeline.gap_checked_video_config(None))
        gap_config = {"visual_mode": "video_mp4",
                      "visual_event_gap_seconds": pipeline.VIDEO_EVENT_MAX_GAP_SECONDS}
        self.assertFalse(pipeline.gap_checked_video_config(gap_config))
        self.assertFalse(pipeline.gap_checked_video_config(
            {**gap_config, "visual_timestamp_validation_version": 2}))
        self.assertFalse(pipeline.gap_checked_video_config(
            {**gap_config, "visual_timestamp_validation_version": 3}))
        self.assertTrue(pipeline.gap_checked_video_config(
            {**gap_config, "visual_timestamp_validation_version": 4}))


class VideoGapFillCallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"fake-mp4")

    def run_mp4(self, responses, duration, cache_dir=None):
        with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid") as upload, \
             mock.patch.object(pipeline, "minimax_chat", side_effect=responses) as chat:
            rows = pipeline.analyze_video_mp4(self.video, "key", "MiniMax-M3", duration=duration,
                                              work_dir=self.root / "work", cache_dir=cache_dir)
        return rows, chat, upload

    def test_second_look_adds_only_new_events_inside_the_gap(self):
        # boundary (20), within a second of a kept event (20.6, 45.6) and out-of-gap (150) are dropped
        fill = answer(20, 20.6, 45, 45.6, 47, 70, 150)
        rows, chat, upload = self.run_mp4([answer(0, 20, 100, 110), fill], 120)
        self.assertEqual([row["timestamp"] for row in rows], [0, 20, 45, 47, 70, 100, 110])
        self.assertEqual((upload.call_count, chat.call_count), (1, 2))
        first, second = chat.call_args_list
        video = second.args[0][0]["content"][1]["video_url"]
        self.assertEqual(video, first.args[0][0]["content"][1]["video_url"])
        self.assertEqual(video["url"], "mm_file://fid")
        self.assertIn("20.0–100.0 秒", second.args[0][0]["content"][0]["text"])
        self.assertEqual(second.kwargs["max_completion_tokens"], first.kwargs["max_completion_tokens"])
        self.assertEqual(second.kwargs["timeout"], first.kwargs["timeout"] // 2)
        for row in rows:
            self.assertEqual((row["analysis_method"], row["completion_status"], row["minimax_file_id"]),
                             ("minimax_video", "complete", "fid"))
            self.assertIs(row["timestamp_reliable"], True)

    def test_added_events_do_not_dilute_first_answer_warnings(self):
        rows, chat, _ = self.run_mp4([answer(1, 2, 3, 7, 35), answer(50)], 70)
        self.assertEqual(chat.call_count, 2)
        by_stamp = {row["timestamp"]: row for row in rows}
        for stamp in (1, 2, 3, 7):
            self.assertIs(by_stamp[stamp]["timestamp_reliable"], False)
            self.assertIn("壓縮", by_stamp[stamp]["timestamp_issue"])
        self.assertIs(by_stamp[50]["timestamp_reliable"], True)

    def test_gap_fill_checks_its_own_compressed_timestamps_before_caching(self):
        cache = self.root / "cache"
        compressed = [1, 2, 3, 4, 5, 6]
        rows, chat, upload = self.run_mp4(
            [answer(40, 90, 137), answer(*compressed)], 166.6, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (1, 2))
        self.assertEqual([row["timestamp"] for row in rows], [*compressed, 40, 90, 137])
        for row in rows:
            if row["timestamp"] in compressed:
                self.assertIs(row["timestamp_reliable"], False)
                self.assertIn("壓縮", row["timestamp_issue"])
                self.assertEqual(row["timestamp_original"], row["timestamp"])
                self.assertFalse(pipeline.visual_time_reliable(row))
            else:
                self.assertIs(row["timestamp_reliable"], True)
        cached, chat, upload = self.run_mp4([], 166.6, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 0))
        self.assertEqual(cached, rows)

    def test_gap_wholly_in_front_tenth_keeps_valid_events_reliable(self):
        # A 40-second opening gap fits inside the first 10% of a 600-second video.
        initial = list(range(40, 551, 30)) + [600]
        for fill in ([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6, 575]):
            with self.subTest(fill=fill):
                rows, chat, _ = self.run_mp4([answer(*initial), answer(*fill)], 600)
                self.assertEqual(chat.call_count, 2)
                self.assertEqual([row["timestamp"] for row in rows], sorted(initial + fill))
                self.assertTrue(all(pipeline.visual_time_reliable(row) for row in rows))
                self.assertTrue(all(row["timestamp_issue"] is None for row in rows))

    def test_discarded_out_of_gap_events_do_not_dilute_compression_check(self):
        compressed = [1, 2, 3, 4, 5, 6]
        rows, _, _ = self.run_mp4(
            [answer(40, 90, 137), answer(*compressed, *([137] * 6))], 166.6)
        self.assertEqual([row["timestamp"] for row in rows], [*compressed, 40, 90, 137])
        for row in rows[:6]:
            self.assertIs(row["timestamp_reliable"], False)
            self.assertIn("壓縮", row["timestamp_issue"])
        self.assertTrue(all(pipeline.visual_time_reliable(row) for row in rows[6:]))

    def test_discarded_duplicates_do_not_create_false_concentration(self):
        duplicates = [1 + index / 100 for index in range(20)]
        rows, _, _ = self.run_mp4(
            [answer(40, 90, 137), answer(*duplicates, 2, 3, 30, 50, 100)], 166.6)
        self.assertEqual([row["timestamp"] for row in rows], [1, 2, 3, 30, 40, 50, 90, 100, 137])
        self.assertTrue(all(pipeline.visual_time_reliable(row) for row in rows))

    def test_summary_only_answer_is_not_gap_filled(self):
        rows, chat, _ = self.run_mp4([json.dumps({"events": [], "overall_summary": "摘要"})], 120)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual([row["screen_type"] for row in rows], ["整片摘要"])

    def test_answer_without_long_gaps_needs_one_call(self):
        rows, chat, _ = self.run_mp4([answer(0, 25, 50, 75, 100)], 100)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(len(rows), 5)

    def test_failed_second_look_keeps_the_complete_first_answer(self):
        failures = [RuntimeError("boom"), pipeline.MiniMaxIncompleteError("length", "MiniMax-M3"),
                    "not json", json.dumps({"events": [event(500)]})]
        for failure in failures:
            with self.subTest(failure=failure):
                cache = self.root / f"cache-{failures.index(failure)}"
                rows, chat, _ = self.run_mp4([answer(0, 100), failure], 120, cache_dir=cache)
                self.assertEqual(chat.call_count, 2)
                self.assertEqual([row["timestamp"] for row in rows], [0, 100])
                self.assertTrue(all(row["completion_status"] == "complete" for row in rows))
                self.assertEqual(len(list(cache.glob("*.json"))), 1)

    def test_cancel_or_deadline_on_second_look_stops_without_caching(self):
        for stop in (pipeline.ExecutionCancelled("cancelled"), pipeline.DeadlineExceeded("deadline")):
            with self.subTest(stop=type(stop).__name__):
                cache = self.root / f"cache-{type(stop).__name__}"
                with self.assertRaises(type(stop)):
                    self.run_mp4([answer(0, 100), stop], 120, cache_dir=cache)
                self.assertEqual(list(cache.glob("*.json")), [])

    def test_budget_block_on_second_look_is_not_swallowed(self):
        blocked = pipeline.budget_blocked_error()("blocked", provider="minimax", operation="chat")
        with self.assertRaises(pipeline.budget_blocked_error()):
            self.run_mp4([answer(0, 100), blocked], 120)

    def test_filled_answer_is_cached_and_reused_without_calls(self):
        cache = self.root / "cache"
        rows, _, _ = self.run_mp4([answer(0, 100), answer(50)], 120, cache_dir=cache)
        self.assertEqual([row["timestamp"] for row in rows], [0, 50, 100])
        again, chat, upload = self.run_mp4([], 120, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 0))
        self.assertEqual([row["timestamp"] for row in again], [0, 50, 100])

    def test_cache_from_before_gap_timestamp_checks_is_not_reused(self):
        cache = self.root / "cache"
        with mock.patch.object(pipeline, "VIDEO_TIMESTAMP_VALIDATION_VERSION", 2, create=True):
            self.run_mp4([answer(0, 100), answer(50)], 120, cache_dir=cache)
        rows, chat, upload = self.run_mp4([answer(0, 100), answer(50)], 120, cache_dir=cache)
        # main reuses the upload receipt even when the analysis cache is invalidated.
        self.assertEqual((upload.call_count, chat.call_count), (0, 2))
        self.assertEqual(len(list(cache.glob("*.json"))), 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 50, 100])

    def test_cache_from_before_gap_window_checks_is_not_reused(self):
        cache = self.root / "cache"
        with mock.patch.object(pipeline, "VIDEO_TIMESTAMP_VALIDATION_VERSION", 3):
            self.run_mp4([answer(0, 100), answer(50)], 120, cache_dir=cache)
        rows, chat, upload = self.run_mp4([answer(0, 100), answer(50)], 120, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 2))
        self.assertEqual(len(list(cache.glob("*.json"))), 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 50, 100])


if __name__ == "__main__":
    unittest.main()
