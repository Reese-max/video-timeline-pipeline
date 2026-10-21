"""MiniMax caps the video input of a single request at roughly 390k tokens.

Measured 2026-10-22 on A1 with one 25.5-minute clip (MiniMax-M3), using the
/v1/responses/input_tokens estimate endpoint plus real chat calls:

  detail=high     345,343 tokens (0.5 fps)   accepted
                  387,558 tokens (0.56 fps)  last estimate that passed
                  0.565 fps and up             rejected (the estimate returns 0)
  detail=default  394,629 tokens (1.99 fps)  last estimate that passed
                  2.0 fps (2290 frames)        rejected

A real chat call at about 400k tokens of high detail is refused within 0.3 s
with HTTP 400 "invalid params, 410 (2013)". max_completion_tokens does not
count against that limit: 400k input plus 524,288 output is still below the
1M context and was still refused. So this is a per-request video input
ceiling, NOT the 1M context window, and the budget has to stay below the
lowest measured pass value.

These tests pin the budget and every request the pipeline builds from it.
Every paid call is mocked here; no test touches the network.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


# The last estimate that still passed, per detail level (2026-10-22, A1).
MEASURED_PASS_TOKENS = {
    "high": 387_558,
    "default": 394_629,
}


def _event(stamp: float) -> dict:
    return {
        "timestamp": stamp,
        "screen_type": "影片片段",
        "visible_text": [],
        "visual_summary": "畫面",
        "important_details": [],
        "confidence": 0.9,
    }


def _answer(*stamps: float) -> str:
    return json.dumps(
        {"events": [_event(stamp) for stamp in stamps], "overall_summary": "摘要"},
        ensure_ascii=False,
    )


class MeasuredInputLimitTests(unittest.TestCase):
    """The budget must stay below what MiniMax was measured to accept."""

    def test_budget_is_below_the_measured_pass_limit(self) -> None:
        budget = pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET
        for detail, measured in MEASURED_PASS_TOKENS.items():
            with self.subTest(detail=detail):
                self.assertLess(budget, measured)

    def test_budget_keeps_about_seven_percent_of_headroom(self) -> None:
        budget = pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET
        lowest = min(MEASURED_PASS_TOKENS.values())
        headroom = (lowest - budget) / lowest
        # 360,000 against 387,558 leaves roughly 7% of room.
        self.assertGreaterEqual(headroom, 0.05)
        self.assertLessEqual(headroom, 0.10)


class SegmentBudgetTests(unittest.TestCase):
    """A segment (and a single whole-video call) must fit the budget."""

    def test_every_detail_fits_its_segment_inside_the_budget(self) -> None:
        budget = pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET
        for detail in ("high", "default", "low"):
            with self.subTest(detail=detail):
                seconds = pipeline.video_mp4_chunk_seconds_limit(1.0, detail)
                per_frame = pipeline.minimax_video_tokens_per_frame(detail)
                self.assertLessEqual(seconds * 1.0 * per_frame, budget)

    def test_high_segment_is_eight_minutes(self) -> None:
        # 360,000 * 0.9 / (616 * 1) / 60 -> 8 whole minutes.
        self.assertEqual(pipeline.video_mp4_chunk_seconds_limit(1.0, "high"), 480)

    def test_a_single_high_call_at_one_fps_covers_under_ten_minutes(self) -> None:
        # At the requested 1 fps the longest video that may go out in one call
        # is 584 s: 585 s would need 360,360 tokens, over the budget.
        per_frame = pipeline.minimax_video_tokens_per_frame("high")
        longest = pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET / per_frame
        self.assertEqual(math.floor(longest), 584)
        self.assertLessEqual(584 * 1.0 * per_frame, pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET)
        self.assertGreater(585 * 1.0 * per_frame, pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET)
        # 584 s still fits in one call; 585 s has to be split.
        self.assertFalse(pipeline.plan_video_mp4_analysis(1.0, 584.0, "high").chunked)
        self.assertTrue(pipeline.plan_video_mp4_analysis(1.0, 585.0, "high").chunked)

    def test_the_failed_25_minute_run_is_split_into_four_segments(self) -> None:
        # The 2026-10-22 A1 run: 25.5 minutes at high/1 fps was refused with
        # HTTP 400 invalid params, 410 (2013) because one segment of 1140 s
        # carried about 700k tokens.
        plan = pipeline.plan_video_mp4_analysis(1.0, 1530.4, "high")
        self.assertTrue(plan.chunked)
        self.assertEqual(plan.chunk_seconds, 480)
        self.assertEqual(plan.chunk_count, 4)
        self.assertEqual(plan.fps, 1.0)
        self.assertEqual(plan.detail, "high")
        self.assertEqual(plan.detail_requested, "high")
        per_frame = pipeline.minimax_video_tokens_per_frame("high")
        self.assertLessEqual(
            plan.chunk_seconds * 1.0 * per_frame,
            pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
        )


class ChunkingOffBudgetTests(unittest.TestCase):
    """With chunking off the fps/detail downgrade has to fit the budget too."""

    DURATIONS = (600.0, 900.0, 1200.0, 1800.0, 2400.0, 3000.0,
                 3600.0, 4800.0, 5400.0, 7200.0, 9000.0, 10000.0)

    def test_the_downgrade_keeps_the_request_inside_the_budget(self) -> None:
        budget = pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET
        for duration in self.DURATIONS:
            with self.subTest(duration=duration):
                with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNKING": "off"}):
                    sampling = pipeline.resolve_video_mp4_sampling(1.0, duration, "high")
                per_frame = pipeline.minimax_video_tokens_per_frame(sampling.detail)
                self.assertLessEqual(sampling.fps * duration * per_frame, budget)

    def test_a_long_high_video_falls_back_to_default_inside_the_budget(self) -> None:
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNKING": "off"}):
            sampling = pipeline.resolve_video_mp4_sampling(1.0, 3000.0, "high")
        self.assertEqual(sampling.detail, "default")
        self.assertEqual(sampling.detail_requested, "high")
        self.assertTrue(sampling.downgraded)
        per_frame = pipeline.minimax_video_tokens_per_frame(sampling.detail)
        self.assertLessEqual(
            sampling.fps * 3000.0 * per_frame,
            pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
        )

    def test_a_video_longer_than_the_floor_admits_the_budget_is_still_exceeded(self) -> None:
        # Even default at the 0.2 fps floor cannot hold a three hour video;
        # the note has to say so instead of pretending the downgrade was enough.
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNKING": "off"}):
            sampling = pipeline.resolve_video_mp4_sampling(1.0, 10800.0, "high")
        self.assertEqual(sampling.detail, "default")
        self.assertIn("仍會超過預算", sampling.note or "")
        self.assertIn("MINIMAX_VIDEO_CHUNKING=on", sampling.note or "")


class SentRequestBudgetTests(unittest.TestCase):
    """The request that actually goes out has to fit the budget."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"fake-mp4")

    def _run(self, duration: float, detail: str, env: dict[str, str] | None = None):
        chat = mock.MagicMock(return_value=_answer(0.0, duration / 2))
        settings = {"ANALYZE_VIDEO_DETAIL": detail}
        settings.update(env or {})
        with mock.patch.dict(os.environ, settings, clear=False), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "minimax_chat", chat), \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.analyze_video_mp4(
                self.video, "key", "MiniMax-M3", duration=duration,
                work_dir=self.root / "work")
        return chat

    @staticmethod
    def _video_part(call) -> dict:
        return call.args[0][0]["content"][1]["video_url"]

    def test_the_request_sent_with_chunking_off_fits_the_budget(self) -> None:
        for duration in (1800.0, 3000.0):
            with self.subTest(duration=duration):
                chat = self._run(duration, "high", {"MINIMAX_VIDEO_CHUNKING": "off"})
                video = self._video_part(chat.call_args_list[0])
                per_frame = pipeline.minimax_video_tokens_per_frame(video["detail"])
                self.assertLessEqual(
                    video["fps"] * duration * per_frame,
                    pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
                )

    def test_a_segment_between_90_and_100_percent_of_the_budget_is_still_sent(self) -> None:
        # The per-segment guard judges the FULL budget, not the 90% the chunk
        # length is derived from, so a keyframe stretch between 90% and 100% of
        # the budget is still allowed through.
        with mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             mock.patch.object(pipeline, "split_video_mp4_into_chunks", return_value=[
                 pipeline.VideoMp4Chunk(self.video, 0.0, 10.6)]), \
             mock.patch.object(pipeline, "video_mp4_chunk_seconds", return_value=60), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "minimax_chat", mock.MagicMock(
                 return_value=_answer(0.0, 5.0))) as chat, \
             mock.patch.dict(os.environ, {"ANALYZE_VIDEO_DETAIL": "default"}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.analyze_video_mp4(
                self.video, "key", "MiniMax-M3", duration=120.0,
                work_dir=self.root / "work")
        chat.assert_called_once()
        # 10.6 s at 179 tokens per frame is 1897 tokens: over 90% of the 2000
        # budget but under the full budget, so it must not be refused.
        self.assertLess(0.9 * 2000, 10.6 * 179)
        self.assertLessEqual(10.6 * 179, 2000)


if __name__ == "__main__":
    unittest.main()
