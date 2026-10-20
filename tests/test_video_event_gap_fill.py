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

    def test_event_at_the_video_end_covers_the_tail_gap(self):
        # issue #105: the prompt prints closed windows, so an event on the window's
        # end boundary (the video end) answers the gap instead of leaving it open.
        rows = [{"timestamp": stamp} for stamp in (0, 30, 60, 100)]
        self.assertEqual(pipeline.video_event_gaps(rows, 100), [])
        self.assertEqual(pipeline.video_event_gaps(rows[:3], 100), [(60.0, 100.0)])
        self.assertEqual(
            pipeline.video_event_gaps([{"timestamp": 60}], 100), [(0.0, 60.0), (60.0, 100.0)])
        self.assertEqual(
            pipeline.video_event_gaps([{"timestamp": 0}, {"timestamp": 100}], 100), [(0.0, 100.0)])

    def test_out_of_range_or_unreliable_ending_does_not_cover_the_tail(self):
        rows = [{"timestamp": stamp} for stamp in (0, 30, 60)]
        for ending in ({"timestamp": 120}, {"timestamp": 100, "timestamp_reliable": False}):
            with self.subTest(ending=ending):
                self.assertEqual(pipeline.video_event_gaps([*rows, ending], 100), [(60.0, 100.0)])


class CliVisualReuseTests(unittest.TestCase):
    def test_visual_json_from_before_gap_filling_is_not_reused(self):
        self.assertFalse(pipeline.gap_checked_video_config({"visual_mode": "video_mp4"}))
        self.assertFalse(pipeline.gap_checked_video_config(None))
        self.assertTrue(pipeline.gap_checked_video_config(
            {"visual_mode": "video_mp4", "visual_event_gap_seconds": pipeline.VIDEO_EVENT_MAX_GAP_SECONDS}))


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
                # issue #105: the 0.0–100.0 秒 window went unanswered, so the result
                # must not be written into the reusable vision cache.
                self.assertEqual(list(cache.glob("*.json")), [])

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
        rows, _, _ = self.run_mp4([answer(0, 100), answer(25, 50, 75)], 120, cache_dir=cache)
        self.assertEqual([row["timestamp"] for row in rows], [0, 25, 50, 75, 100])
        self.assertEqual(pipeline.video_event_gaps(rows, 120), [])
        again, chat, upload = self.run_mp4([], 120, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 0))
        self.assertEqual([row["timestamp"] for row in again], [0, 25, 50, 75, 100])

    def test_answering_a_window_once_does_not_cache_residual_subgaps(self):
        cache = self.root / "cache-residual"
        rows, _, _ = self.run_mp4([answer(0, 100), answer(50)], 120, cache_dir=cache)
        self.assertEqual([row["timestamp"] for row in rows], [0, 50, 100])
        self.assertEqual(pipeline.video_event_gaps(rows, 120), [(0.0, 50.0), (50.0, 100.0)])
        self.assertFalse(pipeline.reusable_video_artifact(rows))
        self.assertEqual(list(cache.glob("*.json")), [])

    def test_fill_keeps_the_ending_event_at_the_window_boundary(self):
        # issue #105: the fill prompt asks about the closed window 60.0–100.0 秒,
        # so an answer at 100 must be kept, the gap must clear, and the filled
        # answer must be cached and reused without a second model call.
        cache = self.root / "cache-endpoint"
        rows, chat, _ = self.run_mp4([answer(0, 30, 60), answer(100)], 100, cache_dir=cache)
        prompt = chat.call_args_list[1].args[0][0]["content"][0]["text"]
        self.assertIn("60.0–100.0 秒", prompt)
        self.assertEqual([row["timestamp"] for row in rows], [0, 30, 60, 100])
        self.assertEqual(pipeline.video_event_gaps(rows, 100), [])
        again, chat2, upload2 = self.run_mp4([], 100, cache_dir=cache)
        self.assertEqual((upload2.call_count, chat2.call_count), (0, 0))
        self.assertEqual([row["timestamp"] for row in again], [0, 30, 60, 100])

    def test_fill_keeps_events_at_both_video_endpoints(self):
        # issue #105: duration 90 with only a mid event; the fill answers 0 and 90
        # (片頭標題與片尾字幕), plus 20 to fill the remaining opening gap.
        cache = self.root / "cache-both-ends"
        rows, _, _ = self.run_mp4([answer(45), answer(0, 20, 90)], 90, cache_dir=cache)
        self.assertEqual([row["timestamp"] for row in rows], [0, 20, 45, 90])
        self.assertEqual(pipeline.video_event_gaps(rows, 90), [])
        again, chat, upload = self.run_mp4([], 90, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 0))
        self.assertEqual([row["timestamp"] for row in again], [0, 20, 45, 90])

    def test_partially_filled_windows_are_not_cached(self):
        # issue #105: the fill answered 0.0–70.0 秒 but left 70.0–120.0 秒 empty,
        # so the result must not land in the reusable vision cache.
        cache = self.root / "cache-partial"
        rows, chat, _ = self.run_mp4([answer(70), answer(30)], 120, cache_dir=cache)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual([row["timestamp"] for row in rows], [30, 70])
        self.assertEqual(pipeline.video_event_gaps(rows, 120), [(30.0, 70.0), (70.0, 120.0)])
        self.assertEqual(list(cache.glob("*.json")), [])

    def test_only_endpoint_fills_keep_both_events_without_caching_the_opening_gap(self):
        cache = self.root / "cache-only-endpoints"
        rows, _, _ = self.run_mp4([answer(45), answer(0, 90)], 90, cache_dir=cache)
        self.assertEqual([row["timestamp"] for row in rows], [0, 45, 90])
        self.assertEqual(pipeline.video_event_gaps(rows, 90), [(0.0, 45.0)])
        self.assertFalse(pipeline.reusable_video_artifact(rows))
        self.assertEqual(list(cache.glob("*.json")), [])

    def test_partially_filled_single_window_is_not_cached(self):
        # Pass timestamp coverage first; one event still leaves a 50-second gap.
        cache = self.root / "cache-partial-single-window"
        rows, chat, _ = self.run_mp4([answer(0, 30, 60), answer(70)], 120, cache_dir=cache)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 30, 60, 70])
        self.assertEqual(pipeline.video_event_gaps(rows, 120), [(70.0, 120.0)])
        self.assertEqual(list(cache.glob("*.json")), [])

    def test_unanswered_windows_survive_visual_json_serialization(self):
        for fill in (RuntimeError("fill failed"), answer(30)):
            with self.subTest(fill=fill):
                rows, _, _ = self.run_mp4([answer(70), fill], 120)
                # CLI saves this list as visual.json and uses this exact reuse gate.
                saved = json.loads(json.dumps(rows))
                self.assertFalse(pipeline.reusable_video_artifact(saved))
                self.assertTrue(all(row["completion_status"] == "complete" for row in saved))
                self.assertTrue(all([70.0, 120.0] in row["unanswered_gap_windows"] for row in saved))

    def test_pre_fix_endpoint_cache_is_recomputed(self):
        cache = self.root / "legacy-cache"
        self.run_mp4([answer(0, 30, 60, 100)], 100, cache_dir=cache)
        cache_path, = cache.glob("*.json")
        legacy = json.loads(cache_path.read_text(encoding="utf-8"))
        legacy["observations"] = [row for row in legacy["observations"] if row["timestamp"] != 100]
        for row in legacy["observations"]:
            row.pop("video_gap_check_version", None)
        pipeline.write_json(cache_path, legacy)
        self.assertFalse(pipeline.reusable_video_artifact(legacy["observations"]))
        rows, chat, _ = self.run_mp4([answer(0, 30, 60), answer(100)], 100, cache_dir=cache)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 30, 60, 100])

    def test_version_one_residual_gap_cache_is_recomputed(self):
        cache = self.root / "version-one-cache"
        complete = [0, 25, 50, 75, 100]
        self.run_mp4([answer(*complete)], 120, cache_dir=cache)
        cache_path, = cache.glob("*.json")
        legacy = json.loads(cache_path.read_text(encoding="utf-8"))
        # The original-window policy stamped version 1 on this sparse result.
        legacy["observations"] = [row for row in legacy["observations"]
                                  if row["timestamp"] in (0, 50, 100)]
        for row in legacy["observations"]:
            row.update(video_gap_check_version=1, unanswered_gap_windows=[])
        pipeline.write_json(cache_path, legacy)
        with self.subTest(layer="CLI visual.json"):
            self.assertFalse(pipeline.reusable_video_artifact(legacy["observations"]))
        rows, chat, _ = self.run_mp4([answer(*complete)], 120, cache_dir=cache)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual([row["timestamp"] for row in rows], complete)
        self.assertTrue(pipeline.reusable_video_artifact(rows))

    def test_clamped_ending_still_gets_a_second_look(self):
        rows, chat, _ = self.run_mp4([answer(0, 30, 60, 120), answer(80)], 100)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 30, 60, 80, 100])
        self.assertIs(rows[-1]["timestamp_reliable"], False)


if __name__ == "__main__":
    unittest.main()
