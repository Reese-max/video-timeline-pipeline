"""Long videos are analysed in segments instead of being downsampled.

The caller asked to keep the requested fps/detail and only to stop the context
window from overflowing, so a video whose frames would cost more than the
token budget is cut into segments (ffmpeg stream copy), every segment is
analysed on its own with the requested fps/detail, and the answers are merged
back onto the original timeline.

Every paid call is mocked here; no test touches the network.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = unittest.skipUnless(FFMPEG, "ffmpeg is required to cut the test video")


def _event(stamp: float, summary: str = "畫面") -> dict:
    return {
        "timestamp": stamp,
        "screen_type": "影片片段",
        "visible_text": [],
        "visual_summary": summary,
        "important_details": [],
        "confidence": 0.9,
    }


def _answer(summary: str = "摘要", *stamps: float) -> str:
    return json.dumps(
        {"events": [_event(stamp) for stamp in stamps], "overall_summary": summary},
        ensure_ascii=False,
    )


class VideoChunkLengthTests(unittest.TestCase):
    """The chunk length and the plan behind it, without touching a video."""

    def test_chunk_length_formula(self) -> None:
        # floor(budget * 0.9 / (tokens per frame * fps) / 60) * 60
        self.assertEqual(pipeline.video_mp4_chunk_seconds_limit(1.0, "high"), 1140)
        self.assertEqual(pipeline.video_mp4_chunk_seconds_limit(1.0, "default"), 4020)
        self.assertEqual(pipeline.video_mp4_chunk_seconds_limit(0.2, "high"), 5820)
        # Every chunk keeps 90% of the budget at the requested fps and detail.
        self.assertLessEqual(
            1140 * 1.0 * pipeline.minimax_video_tokens_per_frame("high"),
            pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
        )

    def test_chunk_seconds_override_may_only_shorten(self) -> None:
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNK_SECONDS": "600"}):
            self.assertEqual(pipeline.video_mp4_chunk_seconds(1.0, "high"), 600)
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNK_SECONDS": "1200"}):
            with self.assertRaises(RuntimeError) as caught:
                pipeline.video_mp4_chunk_seconds(1.0, "high")
        self.assertIn("MINIMAX_VIDEO_CHUNK_SECONDS", str(caught.exception))

    def test_plan_keeps_the_requested_fps_and_detail_when_chunking(self) -> None:
        plan = pipeline.plan_video_mp4_analysis(1.0, 1800.0, "high")
        self.assertTrue(plan.chunked)
        self.assertEqual(plan.fps, 1.0)
        self.assertEqual(plan.detail, "high")
        self.assertEqual(plan.detail_requested, "high")
        self.assertFalse(plan.downgraded)
        self.assertEqual(plan.chunk_seconds, 1140)
        self.assertEqual(plan.chunk_count, 2)

    def test_plan_of_a_short_video_is_a_single_call(self) -> None:
        plan = pipeline.plan_video_mp4_analysis(1.0, 600.0, "high")
        self.assertFalse(plan.chunked)
        self.assertEqual(plan.fps, 1.0)
        self.assertEqual(plan.detail, "high")

    def test_chunking_off_keeps_the_old_downgrade(self) -> None:
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNKING": "off"}):
            plan = pipeline.plan_video_mp4_analysis(1.0, 10800.0, "high")
        self.assertFalse(plan.chunked)
        self.assertEqual(plan.detail, "default")
        self.assertEqual(plan.detail_requested, "high")
        self.assertTrue(plan.downgraded)


@needs_ffmpeg
class VideoChunkingTests(unittest.TestCase):
    """The chunked path end to end; the upload and the chat are mocked."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.work = self.root / "work"
        self.duration = 12.0

    # -- helpers ---------------------------------------------------------
    def _make_video(self, seconds: float = 12.0) -> Path:
        video = self.root / "clip.mp4"
        # One keyframe per second so the stream copy lands on exact boundaries.
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
                "-g", "10", "-pix_fmt", "yuv420p", str(video),
            ],
            check=True,
        )
        return video

    def _env(self, **values: str):
        base = {
            "ANALYZE_VIDEO_FPS": "1",
            "ANALYZE_VIDEO_DETAIL": "default",
            "MINIMAX_VIDEO_CHUNKING": "on",
            "MINIMAX_VIDEO_CHUNK_SECONDS": "",
        }
        base.update(values)
        return mock.patch.dict(os.environ, base, clear=False)

    def _upload(self, fail_for: str | None = None):
        def upload(path: Path, api_key: str) -> str:
            if fail_for and fail_for in Path(path).name:
                raise RuntimeError("boom: 這一段上傳失敗")
            return "fid"
        return mock.patch.object(
            pipeline, "minimax_upload_video_understanding", side_effect=upload)

    def _merged_cache(self) -> dict:
        for path in sorted(self.cache.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("chunked"):
                return data
        raise AssertionError("找不到分段合併後的頂層快取")

    @staticmethod
    def _video_part(call) -> dict:
        return call.args[0][0]["content"][1]["video_url"]

    # -- tests -----------------------------------------------------------
    def test_short_video_keeps_one_call_and_cuts_nothing(self) -> None:
        video = self._make_video()
        chat = mock.MagicMock(side_effect=[_answer("摘要", 0.0, 2.0)])
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             self._env():
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual([row["timestamp"] for row in rows], [0.0, 2.0])
        self.assertFalse((self.work / "minimax-chunks").exists())

    def test_long_video_is_split_and_merged(self) -> None:
        video = self._make_video()
        answers = [
            _answer("第一段", 0.0, 2.0),
            _answer("第二段", 0.0, 2.0),
            _answer("第三段", 0.0, 2.0),
        ]
        chat = mock.MagicMock(side_effect=answers)
        printed = io.StringIO()
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 4000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4", ANALYZE_VIDEO_DETAIL="high"), \
             contextlib.redirect_stdout(printed):
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        # One upload and one call per segment; fps and detail stay as requested.
        self.assertEqual(chat.call_count, 3)
        for call in chat.call_args_list:
            part = self._video_part(call)
            self.assertEqual(part["fps"], 1.0)
            self.assertEqual(part["detail"], "high")
        # The segment answers are moved onto the original timeline.
        stamps = [row["timestamp"] for row in rows]
        self.assertEqual(len(stamps), 6)
        self.assertEqual(stamps, sorted(stamps))
        self.assertAlmostEqual(stamps[0], 0.0)
        self.assertAlmostEqual(stamps[2], 4.2, places=1)
        self.assertAlmostEqual(stamps[4], 8.2, places=1)
        self.assertEqual([row["chunk_index"] for row in rows], [1, 1, 2, 2, 3, 3])
        self.assertEqual(rows[0]["chunk_start"], 0.0)
        self.assertAlmostEqual(rows[2]["chunk_start"], 4.2, places=1)
        self.assertAlmostEqual(rows[4]["chunk_start"], 8.2, places=1)
        # The merged summary names every segment.
        merged = self._merged_cache()
        self.assertIn("第 1 段（00:00–00:04）：第一段", merged["overall_summary"])
        self.assertIn("第 2 段（00:04–00:08）：第二段", merged["overall_summary"])
        self.assertIn("第 3 段（00:08–00:12）：第三段", merged["overall_summary"])
        self.assertEqual(merged["chunk_count"], 3)
        self.assertEqual(merged["chunk_seconds"], 4)
        # The progress line names the segment and its range.
        self.assertIn("MiniMax 分段分析 1/3（00:00–00:04）", printed.getvalue())
        self.assertIn("MiniMax 分段分析 3/3（00:08–00:12）", printed.getvalue())

    def test_failed_segment_propagates_and_the_rerun_reuses_the_first(self) -> None:
        video = self._make_video()
        with mock.patch.object(
                pipeline, "minimax_chat",
                mock.MagicMock(side_effect=[_answer("第一段", 0.0, 2.0)])), \
             self._upload(fail_for="chunk-0001.mp4"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            with self.assertRaises(RuntimeError) as caught:
                pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=self.duration,
                    cache_dir=self.cache, work_dir=self.work)
        self.assertIn("boom", str(caught.exception))
        # The finished first segment is cached; only the rest is sent again.
        chat = mock.MagicMock(side_effect=[_answer("第二段", 0.0, 2.0), _answer("第三段", 0.0, 2.0)])
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             self._upload() as upload, \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(len(rows), 6)

    def test_merged_result_is_cached_at_the_top_level(self) -> None:
        video = self._make_video()
        chat = mock.MagicMock(side_effect=[_answer("第一段", 0.0, 2.0)] * 3)
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        self.assertEqual(len(rows), 6)
        again = mock.MagicMock(side_effect=[])
        with mock.patch.object(pipeline, "minimax_chat", again), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding") as upload, \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            cached = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        again.assert_not_called()
        upload.assert_not_called()
        self.assertEqual([row["timestamp"] for row in cached],
                         [row["timestamp"] for row in rows])

    def test_chunking_off_keeps_the_previous_downgrade(self) -> None:
        video = self._make_video()
        chat = mock.MagicMock(side_effect=[_answer("摘要", 0.0, 2.0)])
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNKING="off"):
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
            expected = pipeline.video_mp4_fps_for_duration(1.0, 12.0, "default")
        self.assertEqual(chat.call_count, 1)
        part = self._video_part(chat.call_args)
        self.assertEqual(part["fps"], expected)
        self.assertLess(part["fps"], 1.0)
        self.assertFalse((self.work / "minimax-chunks").exists())
        self.assertEqual(len(rows), 2)

    def test_invalid_environment_raises_before_any_call(self) -> None:
        video = self._make_video()
        cases = [
            {"MINIMAX_VIDEO_CHUNKING": "maybe"},
            {"MINIMAX_VIDEO_CHUNK_SECONDS": "abc"},
            {"MINIMAX_VIDEO_CHUNK_SECONDS": "0"},
            {"MINIMAX_VIDEO_CHUNK_SECONDS": "-5"},
            {"MINIMAX_VIDEO_CHUNK_SECONDS": "999999"},
        ]
        for values in cases:
            with self.subTest(values=values):
                chat = mock.MagicMock()
                with mock.patch.object(pipeline, "minimax_chat", chat), \
                     mock.patch.object(pipeline, "minimax_upload_video_understanding") as upload, \
                     self._env(**values):
                    with self.assertRaises(RuntimeError) as caught:
                        pipeline.analyze_video_mp4(
                            video, "key", "MiniMax-M3", duration=self.duration,
                            cache_dir=self.cache, work_dir=self.work)
                self.assertIn("MINIMAX_VIDEO_CHUNK", str(caught.exception))
                chat.assert_not_called()
                upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
