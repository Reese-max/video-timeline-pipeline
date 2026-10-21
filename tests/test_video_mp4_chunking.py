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
import math
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import execution_control
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


def _unusable_answer() -> str:
    """A finished answer the pipeline cannot turn into evidence."""
    return json.dumps({"events": [], "overall_summary": ""}, ensure_ascii=False)


class VideoChunkLengthTests(unittest.TestCase):
    """The chunk length and the plan behind it, without touching a video."""

    def test_chunk_length_formula(self) -> None:
        # floor(budget * 0.9 / (tokens per frame * fps) / 60) * 60
        # 360,000 * 0.9 / (616 * 1) / 60 -> 8 whole minutes
        self.assertEqual(pipeline.video_mp4_chunk_seconds_limit(1.0, "high"), 480)
        self.assertEqual(
            pipeline.video_mp4_chunk_seconds_limit(1.0, "default"),
            math.floor(
                pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET
                * pipeline.MINIMAX_VIDEO_CHUNK_BUDGET_RATIO
                / (pipeline.minimax_video_tokens_per_frame("default") * 1.0)
                / 60
            ) * 60,
        )
        self.assertEqual(pipeline.video_mp4_chunk_seconds_limit(0.2, "high"), 2580)
        # Every chunk keeps 90% of the budget at the requested fps and detail.
        self.assertLessEqual(
            480 * 1.0 * pipeline.minimax_video_tokens_per_frame("high"),
            pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
        )

    def test_chunk_seconds_override_may_only_shorten(self) -> None:
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNK_SECONDS": "300"}):
            self.assertEqual(pipeline.video_mp4_chunk_seconds(1.0, "high"), 300)
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNK_SECONDS": "1200"}):
            with self.assertRaises(RuntimeError) as caught:
                pipeline.video_mp4_chunk_seconds(1.0, "high")
        self.assertIn("MINIMAX_VIDEO_CHUNK_SECONDS", str(caught.exception))

    def test_chunk_seconds_override_may_not_be_shorter_than_a_minute(self) -> None:
        # A segment under a minute only buys extra uploads and extra calls.
        for value in ("1", "30", "59"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_CHUNK_SECONDS": value}):
                    with self.assertRaises(RuntimeError) as caught:
                        pipeline.video_mp4_chunk_seconds(1.0, "high")
                self.assertIn("MINIMAX_VIDEO_CHUNK_SECONDS", str(caught.exception))
                self.assertIn("60", str(caught.exception))

    def test_plan_keeps_the_requested_fps_and_detail_when_chunking(self) -> None:
        plan = pipeline.plan_video_mp4_analysis(1.0, 1800.0, "high")
        self.assertTrue(plan.chunked)
        self.assertEqual(plan.fps, 1.0)
        self.assertEqual(plan.detail, "high")
        self.assertEqual(plan.detail_requested, "high")
        self.assertFalse(plan.downgraded)
        self.assertEqual(plan.chunk_seconds, 480)
        self.assertEqual(plan.chunk_count, 4)

    def test_plan_of_a_short_video_is_a_single_call(self) -> None:
        # 500 s at high/1 fps still fits the whole budget in one call.
        plan = pipeline.plan_video_mp4_analysis(1.0, 500.0, "high")
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


class VideoChunkListErrorTests(unittest.TestCase):
    """The segment list is the only truth about where a segment starts."""

    def test_missing_segment_list_raises(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            pipeline.read_video_mp4_chunks(Path("nowhere/chunks.csv"), Path("nowhere"))
        self.assertIn("分段清單不存在", str(caught.exception))

    def test_segment_list_without_usable_rows_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "chunks.csv"
            listing.write_text("filename\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                pipeline.read_video_mp4_chunks(listing, root)
            self.assertIn("沒有任何分段", str(caught.exception))

    def test_malformed_segment_list_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "chunks.csv"
            listing.write_text("chunk-0000.mp4,not-a-number,4.0\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                pipeline.read_video_mp4_chunks(listing, root)
            self.assertIn("分段清單格式錯誤", str(caught.exception))

    def test_segment_list_pointing_at_a_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "chunks.csv"
            listing.write_text("chunk-0000.mp4,0.0,4.0\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                pipeline.read_video_mp4_chunks(listing, root)
            self.assertIn("指向的檔案不存在", str(caught.exception))

    @needs_ffmpeg
    def test_ffmpeg_failure_raises_instead_of_re_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            subprocess.run(
                [
                    FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                    "-g", "10", "-pix_fmt", "yuv420p", str(video),
                ],
                check=True,
            )
            with mock.patch.object(pipeline, "run_external", side_effect=RuntimeError("ffmpeg 失敗")):
                with self.assertRaises(RuntimeError) as caught:
                    pipeline.split_video_mp4_into_chunks(video, root / "chunks", 4, duration=2.0)
            self.assertIn("ffmpeg 失敗", str(caught.exception))


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
            "VIDEO_JOB_DEADLINE": "",
        }
        base.update(values)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, base, clear=False))
        # 60 s is the product floor; these tests need 4 s segments.
        stack.enter_context(mock.patch.object(pipeline, "VIDEO_MP4_MIN_CHUNK_SECONDS", 4))
        return stack

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

    def _chunked_caches(self) -> list[dict]:
        found = []
        for path in sorted(self.cache.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("chunked"):
                found.append(data)
        return found

    def _split_spy(self):
        """Record the segments ffmpeg really produced."""
        seen: list = []
        original = pipeline.split_video_mp4_into_chunks

        def spy(*args, **kwargs):
            result = original(*args, **kwargs)
            seen.extend(result)
            return result

        patcher = mock.patch.object(
            pipeline, "split_video_mp4_into_chunks", side_effect=spy)
        return patcher, seen

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
        patcher, seen = self._split_spy()
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 4000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4", ANALYZE_VIDEO_DETAIL="high"), \
             patcher, \
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
        # The segment answers are moved onto the original timeline. The cut
        # points come from ffmpeg, so they are read back instead of assumed.
        self.assertEqual(len(seen), 3)
        starts = [chunk.start for chunk in seen]
        ends = [chunk.end for chunk in seen]
        stamps = [row["timestamp"] for row in rows]
        self.assertEqual(len(stamps), 6)
        self.assertEqual(stamps, sorted(stamps))
        self.assertAlmostEqual(stamps[0], starts[0], places=1)
        self.assertAlmostEqual(stamps[2], starts[1], places=1)
        self.assertAlmostEqual(stamps[4], starts[2], places=1)
        self.assertEqual([row["chunk_index"] for row in rows], [1, 1, 2, 2, 3, 3])
        self.assertAlmostEqual(rows[0]["chunk_start"], starts[0], places=1)
        self.assertAlmostEqual(rows[2]["chunk_start"], starts[1], places=1)
        self.assertAlmostEqual(rows[4]["chunk_start"], starts[2], places=1)
        # The merged summary names every segment.
        merged = self._merged_cache()
        self.assertIn("第 1 段（00:00–00:04）：第一段", merged["overall_summary"])
        self.assertIn("第 2 段（00:04–00:08）：第二段", merged["overall_summary"])
        self.assertIn("第 3 段（00:08–00:12）：第三段", merged["overall_summary"])
        self.assertEqual(merged["chunk_count"], 3)
        self.assertEqual(merged["chunk_seconds"], 4)
        # The progress line names the segment and its range.
        printed_text = printed.getvalue()
        for index in range(3):
            expected = (
                f"MiniMax 分段分析 {index + 1}/3（"
                f"{pipeline.format_media_clock(starts[index])}–"
                f"{pipeline.format_media_clock(ends[index])}）"
            )
            self.assertIn(expected, printed_text)

    def test_merged_evidence_points_at_the_original_video(self) -> None:
        # The segment files are deleted after the merge, so the evidence may
        # not point at one; the segment name stays for tracing.
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
        for row in rows:
            self.assertEqual(row["source_video"], str(video))
            self.assertTrue(str(row["chunk_source"]).startswith("chunk-"))
            self.assertTrue(str(row["chunk_source"]).endswith(".mp4"))

    def test_timestamp_original_keeps_the_models_own_answer(self) -> None:
        # Only the timeline timestamps move; the model's own value stays.
        video = self._make_video()
        chat = mock.MagicMock(side_effect=[_answer("第一段", 0.0, 2.0)] * 3)
        patcher, seen = self._split_spy()
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"), \
             patcher:
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        starts = [chunk.start for chunk in seen]
        self.assertEqual(len(rows), 6)
        # The second segment's events were answered with 0.0 and 2.0 seconds.
        self.assertAlmostEqual(rows[2]["timestamp"], starts[1], places=1)
        self.assertEqual(rows[2]["timestamp_original"], 0.0)
        self.assertAlmostEqual(rows[3]["timestamp"], starts[1] + 2.0, places=1)
        self.assertEqual(rows[3]["timestamp_original"], 2.0)

    def test_successful_merge_deletes_the_segment_files(self) -> None:
        # The segments are worthless once merged and a rerun cuts them again.
        video = self._make_video()
        chat = mock.MagicMock(side_effect=[_answer("第一段", 0.0, 2.0)] * 3)
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        self.assertFalse((self.work / "minimax-chunks").exists())

    def test_failed_run_keeps_the_segment_files(self) -> None:
        # A failed run may keep them; the next run cuts them again anyway.
        video = self._make_video()
        with mock.patch.object(
                pipeline, "minimax_chat",
                mock.MagicMock(side_effect=[_answer("第一段", 0.0, 2.0)])), \
             self._upload(fail_for="chunk-0001.mp4"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            with self.assertRaises(RuntimeError):
                pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=self.duration,
                    cache_dir=self.cache, work_dir=self.work)
        chunk_dir = self.work / "minimax-chunks"
        self.assertTrue(chunk_dir.is_dir())
        self.assertTrue(any(chunk_dir.glob("chunk-*.mp4")))

    def test_recutting_the_same_source_produces_the_same_bytes(self) -> None:
        # A rerun must hit the per-segment cache, so the stream copy has to
        # write the very same bytes for the very same source.
        video = self._make_video()
        first = pipeline.split_video_mp4_into_chunks(
            video, self.root / "split-a", 4, duration=self.duration)
        second = pipeline.split_video_mp4_into_chunks(
            video, self.root / "split-b", 4, duration=self.duration)
        self.assertEqual(len(first), len(second))
        self.assertEqual([c.start for c in first], [c.start for c in second])
        for left, right in zip(first, second):
            self.assertEqual(
                pipeline.file_sha256(left.path), pipeline.file_sha256(right.path))

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

    def test_incomplete_segment_is_not_cached_at_the_top_level(self) -> None:
        # An unusable answer must not become a reusable cache entry.
        video = self._make_video()
        chat = mock.MagicMock(side_effect=[_unusable_answer()] * 3)
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            rows = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=self.duration,
                cache_dir=self.cache, work_dir=self.work)
        self.assertEqual(len(rows), 3)
        self.assertEqual(self._chunked_caches(), [])
        for row in rows:
            self.assertFalse(pipeline.usable_visual_observation(row))

    def test_a_segment_over_the_budget_is_never_sent(self) -> None:
        # Keyframes can stretch a segment past the budget; that segment must
        # not be uploaded or analysed.
        video = self._make_video()
        chat = mock.MagicMock()
        upload = mock.MagicMock()
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", upload), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             mock.patch.object(pipeline, "video_mp4_chunk_seconds", return_value=60), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            with self.assertRaises(RuntimeError) as caught:
                pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=self.duration,
                    cache_dir=self.cache, work_dir=self.work)
        self.assertIn("超出", str(caught.exception))
        chat.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(self._chunked_caches(), [])

    def test_cancelled_job_stops_before_the_next_segment(self) -> None:
        # A cancelled job must not pay for one more segment.
        self.addCleanup(execution_control.reset_execution_cancellation)
        video = self._make_video()
        seen: list[int] = []

        def chat_side_effect(*args, **kwargs):
            seen.append(1)
            execution_control.cancel_execution()
            return _answer("第一段", 0.0, 2.0)

        chat = mock.MagicMock(side_effect=chat_side_effect)
        upload = mock.MagicMock(return_value="fid")
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", upload), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            with self.assertRaises(execution_control.ExecutionCancelled):
                pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=self.duration,
                    cache_dir=self.cache, work_dir=self.work)
        self.assertEqual(len(seen), 1)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(upload.call_count, 1)

    def test_expired_deadline_stops_before_the_next_segment(self) -> None:
        video = self._make_video()
        seen: list[int] = []

        def chat_side_effect(*args, **kwargs):
            seen.append(1)
            os.environ["VIDEO_JOB_DEADLINE"] = str(time.time() - 5)
            return _answer("第一段", 0.0, 2.0)

        chat = mock.MagicMock(side_effect=chat_side_effect)
        upload = mock.MagicMock(return_value="fid")
        with mock.patch.object(pipeline, "minimax_chat", chat), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", upload), \
             mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 2000), \
             self._env(MINIMAX_VIDEO_CHUNK_SECONDS="4"):
            with self.assertRaises(execution_control.DeadlineExceeded):
                pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=self.duration,
                    cache_dir=self.cache, work_dir=self.work)
        self.assertEqual(len(seen), 1)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(upload.call_count, 1)

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
            {"MINIMAX_VIDEO_CHUNK_SECONDS": "1"},
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
