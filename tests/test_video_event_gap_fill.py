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
        self.assertFalse(pipeline.reusable_video_artifact(rows))
        self.assertEqual(list(cache.glob("*.json")), [])
        again, chat, upload = self.run_mp4([answer(40, 90, 137), answer(*compressed)], 166.6, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 2))
        self.assertEqual(again, rows)

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

    def test_failed_second_look_keeps_first_evidence_without_caching_coverage(self):
        failures = [RuntimeError("boom"), pipeline.MiniMaxIncompleteError("length", "MiniMax-M3"),
                    "not json", json.dumps({"events": [event(500)]})]
        for failure in failures:
            with self.subTest(failure=failure):
                cache = self.root / f"cache-{failures.index(failure)}"
                rows, chat, _ = self.run_mp4([answer(0, 100), failure], 120, cache_dir=cache)
                self.assertEqual(chat.call_count, 2)
                self.assertEqual([row["timestamp"] for row in rows], [0, 100])
                self.assertTrue(all(row["completion_status"] == "complete" for row in rows))
                self.assertEqual(list(cache.glob("*.json")), [])
                self.assertTrue(pipeline.visual_gaps_pending(rows))

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
        again, chat, upload = self.run_mp4([], 120, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 0))
        self.assertEqual([row["timestamp"] for row in again], [0, 25, 50, 75, 100])

    def test_cache_from_before_gap_timestamp_checks_is_not_reused(self):
        cache = self.root / "cache"
        with mock.patch.object(pipeline, "VIDEO_TIMESTAMP_VALIDATION_VERSION", 2, create=True):
            self.run_mp4([answer(0, 100), answer(25, 50, 75)], 120, cache_dir=cache)
        rows, chat, upload = self.run_mp4([answer(0, 100), answer(25, 50, 75)], 120, cache_dir=cache)
        # main reuses the upload receipt even when the analysis cache is invalidated.
        self.assertEqual((upload.call_count, chat.call_count), (0, 2))
        self.assertEqual(len(list(cache.glob("*.json"))), 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 25, 50, 75, 100])

    def test_cache_from_before_gap_window_checks_is_not_reused(self):
        cache = self.root / "cache"
        with mock.patch.object(pipeline, "VIDEO_TIMESTAMP_VALIDATION_VERSION", 3):
            self.run_mp4([answer(0, 100), answer(25, 50, 75)], 120, cache_dir=cache)
        rows, chat, upload = self.run_mp4([answer(0, 100), answer(25, 50, 75)], 120, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 2))
        self.assertEqual(len(list(cache.glob("*.json"))), 2)
        self.assertEqual([row["timestamp"] for row in rows], [0, 25, 50, 75, 100])

    def test_endpoint_is_kept_without_exempting_a_forty_second_tail(self):
        cache = self.root / "endpoint"
        rows, chat, _ = self.run_mp4([answer(0, 30, 60), answer(100)], 100, cache_dir=cache)
        self.assertIn("60.0–100.0 秒", chat.call_args_list[1].args[0][0]["content"][0]["text"])
        self.assertEqual([row["timestamp"] for row in rows], [0, 30, 60, 100])
        self.assertEqual(pipeline.video_event_gaps(rows, 100), [(60.0, 100.0)])
        self.assertEqual(list(cache.glob("*.json")), [])
        rows, chat, _ = self.run_mp4([answer(0, 30, 60), answer(80, 100)], 100, cache_dir=cache)
        self.assertEqual(pipeline.video_event_gaps(rows, 100), [])
        self.assertTrue(pipeline.reusable_video_artifact(rows))
        again, chat, upload = self.run_mp4([], 100, cache_dir=cache)
        self.assertEqual((upload.call_count, chat.call_count), (0, 0))
        self.assertEqual(again, rows)

    def test_both_endpoint_events_and_bounded_internal_events_are_retained(self):
        cache = self.root / "both-endpoints"
        rows, _, _ = self.run_mp4([answer(45), answer(0, 20, 70, 90)], 90, cache_dir=cache)
        self.assertEqual([row["timestamp"] for row in rows], [0, 20, 45, 70, 90])
        self.assertEqual(pipeline.video_event_gaps(rows, 90), [])
        self.assertTrue(pipeline.reusable_video_artifact(rows))

    def test_any_residual_subgap_remains_pending_after_one_fill(self):
        for initial, fill, duration, expected in (
            ((0, 100), (50,), 120, [(0.0, 50.0), (50.0, 100.0)]),
            ((0, 1, 100), (100,), 100, [(1.0, 100.0)]),
            ((45,), (0, 90), 90, [(0.0, 45.0), (45.0, 90.0)]),
        ):
            with self.subTest(initial=initial, fill=fill):
                cache = self.root / f"partial-{initial}"
                rows, _, _ = self.run_mp4([answer(*initial), answer(*fill)], duration, cache_dir=cache)
                self.assertEqual(pipeline.video_event_gaps(rows, duration), expected)
                self.assertEqual(list(cache.glob("*.json")), [])
                saved = json.loads(json.dumps(rows))
                self.assertTrue(pipeline.visual_gaps_pending(saved))
                self.assertFalse(pipeline.reusable_video_artifact(saved))
                self.assertTrue(all(row["unanswered_gap_windows"] == [list(gap) for gap in expected]
                                    for row in saved))

    def test_version_two_unbounded_tail_cache_is_recomputed(self):
        cache = self.root / "old-tail-cache"
        self.run_mp4([answer(0, 25, 50, 75, 100)], 120, cache_dir=cache)
        cache_path, = cache.glob("*.json")
        legacy = json.loads(cache_path.read_text())
        legacy["observations"] = [row for row in legacy["observations"] if row["timestamp"] in (0, 100)]
        for row in legacy["observations"]:
            row.update(video_gap_check_version=2, unanswered_gap_windows=[])
        cache_path.write_text(json.dumps(legacy))
        self.assertFalse(pipeline.reusable_video_artifact(legacy["observations"]))
        rows, chat, _ = self.run_mp4([answer(0, 25, 50, 75, 100)], 120, cache_dir=cache)
        self.assertEqual(chat.call_count, 1)
        self.assertTrue(pipeline.reusable_video_artifact(rows))

    def test_unreliable_events_cannot_fill_a_coverage_gap(self):
        rows = [{"timestamp": stamp, "timestamp_reliable": reliable}
                for stamp, reliable in ((0, True), (30, False), (60, False), (90, True))]
        self.assertEqual(pipeline.video_event_gaps(rows, 90), [(0.0, 90.0)])

    def test_individually_valid_chunks_do_not_hide_a_gap_at_the_join(self):
        cache = self.root / "chunk-join"
        paths = [self.root / f"part-{index}.mp4" for index in range(2)]
        for path in paths:
            path.write_bytes(b"synthetic fixture; provider is mocked")
        chunks = [pipeline.VideoMp4Chunk(paths[0], 0, 60), pipeline.VideoMp4Chunk(paths[1], 60, 120)]
        plan = pipeline.VideoMp4ChunkPlan(True, 60, 2, 1, "default", "default", False, None)
        with mock.patch.object(pipeline, "split_video_mp4_into_chunks", return_value=chunks), \
                mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
                mock.patch.object(pipeline, "minimax_chat", side_effect=[answer(0, 30), answer(30, 60)]):
            rows = pipeline._analyze_video_mp4_chunked(self.video, "key", "MiniMax-M3",
                duration=120, cache_dir=cache, fps_value=1, detail_value="default", timeout=60,
                work_dir=self.root / "work", plan=plan, max_completion_tokens=1000)
        self.assertEqual([row["timestamp"] for row in rows], [0, 30, 90, 120])
        self.assertTrue(all(row["unanswered_gap_windows"] == [[30.0, 90.0]] for row in rows))
        self.assertFalse(pipeline.reusable_video_artifact(rows))
        self.assertFalse(any(json.loads(path.read_text()).get("chunked") for path in cache.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
