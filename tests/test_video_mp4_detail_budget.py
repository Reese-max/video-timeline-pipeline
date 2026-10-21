"""The video detail level decides how many tokens a frame costs.

Measured 2026-10-22 on A1 with one 59.2s video (fps=1, MiniMax-M3):
detail=default spent 10,567 prompt tokens (~179/frame) and detail=high spent
36,367 (~616/frame, 3.44x). The fps ceiling therefore has to follow the
requested detail, and a video that is too long even for the 0.2 fps floor has
to fall back to detail=default instead of overflowing the context window.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
import pipeline


def _event(stamp: float, summary: str = "畫面") -> dict:
    return {
        "timestamp": stamp,
        "screen_type": "影片片段",
        "visible_text": [],
        "visual_summary": summary,
        "important_details": [],
        "confidence": 0.9,
    }


def _answer(*stamps: float) -> str:
    return json.dumps(
        {"events": [_event(stamp) for stamp in stamps], "overall_summary": "摘要"},
        ensure_ascii=False,
    )


class VideoDetailTokenCostTests(unittest.TestCase):
    def test_tokens_per_frame_follow_the_detail_level(self) -> None:
        self.assertEqual(pipeline.minimax_video_tokens_per_frame("default"), 179)
        self.assertEqual(pipeline.minimax_video_tokens_per_frame("high"), 616)
        # detail=low was never measured: it conservatively reuses the default cost.
        self.assertEqual(pipeline.minimax_video_tokens_per_frame("low"), 179)
        self.assertEqual(pipeline.minimax_video_tokens_per_frame("HIGH"), 616)

    def test_default_fps_ceiling_is_unchanged(self) -> None:
        fps = pipeline.video_mp4_fps_for_duration(1.0, 7200.0)
        self.assertLess(fps, 1.0)
        self.assertLessEqual(
            fps * 7200.0 * pipeline.MINIMAX_VIDEO_TOKENS_PER_FRAME,
            pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
        )
        self.assertEqual(pipeline.video_mp4_fps_for_duration(1.0, None), 1.0)
        self.assertEqual(pipeline.video_mp4_fps_for_duration(1.0, 10 ** 6), 0.2)

    def test_high_keeps_full_fps_for_a_short_video(self) -> None:
        self.assertEqual(pipeline.video_mp4_fps_for_duration(1.0, 59.0, "high"), 1.0)

    def test_high_thirty_minutes_drops_fps_but_stays_inside_the_budget(self) -> None:
        fps = pipeline.video_mp4_fps_for_duration(1.0, 1800.0, "high")
        self.assertEqual(fps, 0.721)
        self.assertLessEqual(1800.0 * fps * 616, pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET)

    def test_high_video_too_long_for_the_floor_falls_back_to_default(self) -> None:
        sampling = pipeline.resolve_video_mp4_sampling(1.0, 10800.0, "high")
        self.assertEqual(sampling.detail, "default")
        self.assertEqual(sampling.detail_requested, "high")
        self.assertTrue(sampling.downgraded)
        self.assertEqual(
            sampling.fps, pipeline.video_mp4_fps_for_duration(1.0, 10800.0, "default"))
        self.assertIn("降為 default", sampling.note or "")

    def test_default_is_never_downgraded(self) -> None:
        sampling = pipeline.resolve_video_mp4_sampling(1.0, 36000.0, "default")
        self.assertEqual(sampling.detail, "default")
        self.assertFalse(sampling.downgraded)
        self.assertEqual(sampling.fps, 0.2)
        self.assertIsNone(sampling.note)

    def test_low_keeps_its_own_detail_because_default_costs_the_same(self) -> None:
        sampling = pipeline.resolve_video_mp4_sampling(1.0, 36000.0, "low")
        self.assertEqual(sampling.detail, "low")
        self.assertFalse(sampling.downgraded)


class VideoDetailCallSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"fake-mp4")

    def _run(self, duration: float, detail: str, responses: list[str] | None = None):
        chat = mock.MagicMock(side_effect=responses or [_answer(0.0, duration / 2)])
        printed = io.StringIO()
        with mock.patch.dict(os.environ, {"ANALYZE_VIDEO_DETAIL": detail}, clear=False), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "minimax_chat", chat), \
             contextlib.redirect_stdout(printed):
            pipeline.analyze_video_mp4(
                self.video, "key", "MiniMax-M3", duration=duration, work_dir=self.root / "work")
        return chat, printed.getvalue()

    @staticmethod
    def _video_part(call) -> dict:
        return call.args[0][0]["content"][1]["video_url"]

    def test_payload_uses_the_requested_detail_and_its_fps(self) -> None:
        chat, _ = self._run(1800.0, "high")
        video = self._video_part(chat.call_args_list[0])
        self.assertEqual(video["detail"], "high")
        self.assertEqual(video["fps"], 0.721)

    def test_short_high_detail_video_still_gets_full_fps(self) -> None:
        chat, printed = self._run(59.0, "high")
        video = self._video_part(chat.call_args_list[0])
        self.assertEqual(video["detail"], "high")
        self.assertEqual(video["fps"], 1.0)
        self.assertNotIn("降為 default", printed)

    def test_very_long_video_is_sent_with_default_detail(self) -> None:
        chat, printed = self._run(10800.0, "high")
        video = self._video_part(chat.call_args_list[0])
        self.assertEqual(video["detail"], "default")
        self.assertEqual(
            video["fps"], pipeline.video_mp4_fps_for_duration(1.0, 10800.0, "default"))
        self.assertIn("降為 default", printed)
        self.assertIn("影片過長", printed)

    def test_gap_fill_call_uses_the_same_detail_and_fps(self) -> None:
        chat, _ = self._run(
            1800.0, "high", responses=[_answer(0.0, 100.0), _answer(50.0)])
        self.assertEqual(chat.call_count, 2)
        first = self._video_part(chat.call_args_list[0])
        second = self._video_part(chat.call_args_list[1])
        self.assertEqual(first["detail"], "high")
        self.assertEqual(first, second)
        self.assertEqual(second["fps"], 0.721)

    def test_invalid_detail_still_raises_the_known_error(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self._run(60.0, "ultra")
        self.assertIn("ANALYZE_VIDEO_DETAIL", str(caught.exception))


class McpSamplingStatsTests(unittest.TestCase):
    JOB_ID = "0123456789ab"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.job_dir = self.root / "mcp_jobs" / self.JOB_ID
        self.job_dir.mkdir(parents=True)
        self.source = self.job_dir / "source.mp4"
        self.source.write_bytes(b"fake-mp4-bytes")
        manifest = {
            "status": "ready",
            "segment_seconds": 120,
            "source": {"title": "demo", "url": "https://example.com/video"},
        }
        (self.job_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def _run(self, duration: float, detail: str):
        chat = mock.MagicMock(return_value=_answer(0.0, duration / 2))
        patches = [
            mock.patch.object(mcp_server, "MCP_ROOT", self.root),
            mock.patch.object(mcp_server, "MCP_JOBS_ROOT", self.root / "mcp_jobs"),
            mock.patch.dict(mcp_server.os.environ, {"ANALYZE_VIDEO_DETAIL": detail}, clear=False),
            mock.patch.multiple(mcp_server, check_job_capacity=mock.MagicMock(),
                                storage_policy=mock.MagicMock()),
            mock.patch.object(mcp_server.pipeline, "probe_video", return_value={
                "format": {"duration": str(duration)},
                "streams": [{"codec_type": "video"}],
            }),
            mock.patch.object(mcp_server.pipeline, "minimax_model", return_value="MiniMax-M3"),
            mock.patch.object(mcp_server.pipeline, "minimax_endpoint",
                              return_value="https://api.minimax.io/v1"),
            mock.patch.object(mcp_server.pipeline, "minimax_subscription_key", return_value="key"),
            mock.patch.object(mcp_server.pipeline, "minimax_upload_video_understanding",
                              return_value="fid"),
            mock.patch.object(mcp_server.pipeline, "minimax_chat", chat),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        result = mcp_server.analyze_prepared_sync(self.JOB_ID)
        return result, chat

    def test_sampling_stats_match_the_request_that_was_sent(self) -> None:
        result, chat = self._run(1800.0, "high")
        sampling = result["sampling"]
        video = chat.call_args.args[0][0]["content"][1]["video_url"]
        self.assertEqual(sampling["detail"], "high")
        self.assertEqual(sampling["detail_requested"], "high")
        self.assertLess(sampling["fps"], 1.0)
        self.assertEqual(sampling["fps"], video["fps"])
        self.assertEqual(sampling["detail"], video["detail"])

    def test_sampling_stats_record_the_downgrade_of_a_very_long_video(self) -> None:
        result, chat = self._run(10800.0, "high")
        sampling = result["sampling"]
        video = chat.call_args.args[0][0]["content"][1]["video_url"]
        self.assertEqual(sampling["detail"], "default")
        self.assertEqual(sampling["detail_requested"], "high")
        self.assertEqual(sampling["detail"], video["detail"])
        self.assertEqual(sampling["fps"], video["fps"])
        self.assertEqual(
            sampling["fps"], pipeline.video_mp4_fps_for_duration(1.0, 10800.0, "default"))


if __name__ == "__main__":
    unittest.main()
