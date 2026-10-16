"""Unit tests for ANALYZE_VISUAL_MODE and >512MB video_mp4 compression (no frame fallback)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def _good_video_response(timestamps: tuple[float, ...] = (1.0,)) -> str:
    return json.dumps(
        {
            "events": [
                {
                    "timestamp": stamp,
                    "screen_type": "開場",
                    "visible_text": ["Hello"],
                    "visual_summary": "測試畫面摘要",
                    "important_details": ["細節"],
                    "confidence": 0.9,
                }
                for stamp in timestamps
            ],
            "overall_summary": "整片摘要",
        },
        ensure_ascii=False,
    )


class VisualModeTests(unittest.TestCase):
    def test_default_is_video_mp4(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANALYZE_VISUAL_MODE", None)
            self.assertEqual(pipeline.visual_mode(), "video_mp4")

    def test_aliases(self) -> None:
        for raw, expected in [
            ("video_mp4", "video_mp4"),
            ("VIDEO", "video_mp4"),
            ("mp4", "video_mp4"),
        ]:
            with mock.patch.dict(os.environ, {"ANALYZE_VISUAL_MODE": raw}):
                self.assertEqual(pipeline.visual_mode(), expected)

    def test_invalid_raises(self) -> None:
        with mock.patch.dict(os.environ, {"ANALYZE_VISUAL_MODE": "audio"}):
            with self.assertRaises(RuntimeError):
                pipeline.visual_mode()


class UploadStrategyTests(unittest.TestCase):
    def test_under_limit_is_direct(self) -> None:
        limit = pipeline.MINIMAX_VIDEO_FILES_API_LIMIT_BYTES
        self.assertEqual(pipeline.choose_video_mp4_upload_strategy(limit), "direct")
        self.assertEqual(pipeline.choose_video_mp4_upload_strategy(limit - 1), "direct")

    def test_over_limit_prefers_compress_first(self) -> None:
        limit = pipeline.MINIMAX_VIDEO_FILES_API_LIMIT_BYTES
        self.assertEqual(pipeline.choose_video_mp4_upload_strategy(limit + 1), "compress_first")
        self.assertEqual(
            pipeline.choose_video_mp4_upload_strategy(600 * 1024 * 1024),
            "compress_first",
        )


class SizeErrorDetectionTests(unittest.TestCase):
    def test_known_size_messages(self) -> None:
        self.assertTrue(
            pipeline.is_minimax_video_size_error(
                RuntimeError("影片 600000000 bytes 超過 MiniMax Files API 上限 512 MB，無法直接上傳理解。")
            )
        )
        self.assertTrue(pipeline.is_minimax_video_size_error(RuntimeError("API HTTP 413：entity too large")))
        self.assertTrue(pipeline.is_minimax_video_size_error(RuntimeError("file size limit exceeded")))

    def test_unrelated_errors(self) -> None:
        self.assertFalse(pipeline.is_minimax_video_size_error(RuntimeError("API HTTP 401：unauthorized")))
        self.assertFalse(pipeline.is_minimax_video_size_error(RuntimeError("network timeout")))

    def test_rate_limit_and_budget_errors_are_not_size_errors(self) -> None:
        from cost_tracker import BudgetBlockedError

        self.assertFalse(
            pipeline.is_minimax_video_size_error(RuntimeError("API HTTP 429: rate limit exceeded"))
        )
        self.assertFalse(
            pipeline.is_minimax_video_size_error(
                RuntimeError("Budget blocked: projected (1.200 USD) exceeds daily limit (1.00 USD)")
            )
        )
        self.assertFalse(
            pipeline.is_minimax_video_size_error(RuntimeError("rate limit exceeded"))
        )
        self.assertFalse(
            pipeline.is_minimax_video_size_error(RuntimeError("exceeds daily limit"))
        )
        self.assertFalse(
            pipeline.is_minimax_video_size_error(RuntimeError("API HTTP 429: too many requests"))
        )
        self.assertFalse(
            pipeline.is_minimax_video_size_error(RuntimeError("monthly quota exceeded"))
        )
        self.assertFalse(
            pipeline.is_minimax_video_size_error(
                BudgetBlockedError(
                    "Budget blocked: projected (1.200 USD) exceeds daily limit (1.00 USD)",
                    provider="minimax",
                    operation="files.upload",
                )
            )
        )


class AnalyzeVideoMp4FallbackTests(unittest.TestCase):
    def _touch_fake_mp4(self, path: Path, size: int) -> Path:
        path.write_bytes(b"\x00" * min(size, 64))
        if size > 64:
            # Avoid allocating huge files: mock st_size via a wrapper path is hard,
            # so tests that need >512MB patch Path.stat instead.
            pass
        return path

    def test_success_path_unchanged_under_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"fake-mp4-bytes")
            with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid-1") as upload, \
                 mock.patch.object(pipeline, "minimax_chat", return_value=_good_video_response()) as chat, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress:
                observations = pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=5.0, work_dir=root / "work"
                )
            upload.assert_called_once()
            chat.assert_called_once()
            compress.assert_not_called()
            self.assertEqual(observations[0]["analysis_method"], "minimax_video")
            self.assertEqual(observations[0]["minimax_file_id"], "fid-1")
            self.assertEqual(observations[0]["visual_summary"], "測試畫面摘要")

    def test_oversize_chooses_compress_then_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "huge.mp4"
            video.write_bytes(b"fake")
            compressed = root / "work" / "minimax-compressed.mp4"

            def fake_compress(src, dest, **kwargs):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"compressed-mp4")
                return dest

            real_stat = Path.stat

            def fake_stat(self, *args, **kwargs):
                result = real_stat(self, *args, **kwargs)
                if self == video:
                    return mock.Mock(
                        st_size=pipeline.MINIMAX_VIDEO_FILES_API_LIMIT_BYTES + 10,
                        st_mtime_ns=result.st_mtime_ns,
                        st_mode=result.st_mode,
                    )
                return result

            with mock.patch.object(Path, "stat", fake_stat), \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=fake_compress) as compress, \
                 mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid-c") as upload, \
                 mock.patch.object(pipeline, "minimax_chat", return_value=_good_video_response((1.0, 118.5))):
                observations = pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=120.0, work_dir=root / "work"
                )
            compress.assert_called()
            upload.assert_called_once()
            self.assertEqual(observations[0]["analysis_method"], "minimax_video_compressed")
            self.assertEqual(observations[0]["minimax_file_id"], "fid-c")
            self.assertIn("compressed_from_bytes", observations[0])
            self.assertTrue(all(pipeline.usable_visual_observation(item) for item in observations))
            self.assertEqual(
                sorted(item["timestamp"] for item in observations), [1.0, 118.5]
            )

    def test_upload_size_failure_triggers_compress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"fake-mp4")

            def fake_compress(src, dest, **kwargs):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"compressed")
                return dest

            with mock.patch.object(
                pipeline,
                "minimax_upload_video_understanding",
                side_effect=[
                    RuntimeError("API HTTP 413：payload too large"),
                    "fid-retry",
                ],
            ) as upload, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=fake_compress) as compress, \
                 mock.patch.object(pipeline, "minimax_chat", return_value=_good_video_response()):
                observations = pipeline.analyze_video_mp4(
                    video, "key", "MiniMax-M3", duration=10.0, work_dir=root / "work"
                )
            self.assertEqual(upload.call_count, 2)
            compress.assert_called()
            self.assertEqual(observations[0]["analysis_method"], "minimax_video_compressed")

    def test_compress_failure_raises_without_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "huge.mp4"
            video.write_bytes(b"x")
            real_stat = Path.stat

            def fake_stat(self, *args, **kwargs):
                result = real_stat(self, *args, **kwargs)
                if self == video:
                    return mock.Mock(
                        st_size=pipeline.MINIMAX_VIDEO_FILES_API_LIMIT_BYTES + 1,
                        st_mtime_ns=result.st_mtime_ns,
                        st_mode=result.st_mode,
                    )
                return result

            with mock.patch.object(Path, "stat", fake_stat), \
                 mock.patch.object(
                     pipeline,
                     "compress_video_for_minimax_upload",
                     side_effect=RuntimeError("找不到 ffmpeg，請先安裝並加入 PATH。"),
                 ), \
                 mock.patch.object(pipeline, "minimax_upload_video_understanding") as upload:
                with self.assertRaises(RuntimeError) as ctx:
                    pipeline.analyze_video_mp4(
                        video, "key", "MiniMax-M3", duration=30.0, work_dir=root / "work"
                    )
            upload.assert_not_called()
            self.assertIn("抽幀備援已移除", str(ctx.exception))
            self.assertIn("ffmpeg", str(ctx.exception))

    def test_upload_rate_limit_exceeded_raises_without_compress_or_frames_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"fake-mp4")
            with mock.patch.object(
                pipeline,
                "minimax_upload_video_understanding",
                side_effect=RuntimeError("API HTTP 429: rate limit exceeded"),
            ) as upload, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress:
                with self.assertRaises(RuntimeError) as ctx:
                    pipeline.analyze_video_mp4(
                        video, "key", "MiniMax-M3", duration=8.0, work_dir=root / "work"
                    )
                self.assertIn("rate limit exceeded", str(ctx.exception))
            self.assertEqual(upload.call_count, 1)
            compress.assert_not_called()

    def test_upload_budget_blocked_raises_without_compress_or_frames_fallback(self) -> None:
        from cost_tracker import BudgetBlockedError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"fake-mp4")
            with mock.patch.object(
                pipeline,
                "minimax_upload_video_understanding",
                side_effect=BudgetBlockedError(
                    "Budget blocked: projected (1.200 USD) exceeds daily limit (1.00 USD)",
                    provider="minimax",
                    operation="files.upload",
                ),
            ) as upload, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress:
                with self.assertRaises(BudgetBlockedError) as ctx:
                    pipeline.analyze_video_mp4(
                        video, "key", "MiniMax-M3", duration=8.0, work_dir=root / "work"
                    )
                self.assertIn("exceeds daily limit", str(ctx.exception))
            self.assertEqual(upload.call_count, 1)
            compress.assert_not_called()

    def test_oversize_upload_rate_limit_raises_without_retry_or_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "huge.mp4"
            video.write_bytes(b"fake")

            def fake_compress(src, dest, **kwargs):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"compressed")
                return dest

            real_stat = Path.stat

            def fake_stat(self, *args, **kwargs):
                result = real_stat(self, *args, **kwargs)
                if self == video:
                    return mock.Mock(
                        st_size=pipeline.MINIMAX_VIDEO_FILES_API_LIMIT_BYTES + 10,
                        st_mtime_ns=result.st_mtime_ns,
                        st_mode=result.st_mode,
                    )
                return result

            with mock.patch.object(Path, "stat", fake_stat), \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=fake_compress) as compress, \
                 mock.patch.object(
                     pipeline,
                     "minimax_upload_video_understanding",
                     side_effect=RuntimeError("API HTTP 429: rate limit exceeded"),
                 ) as upload:
                with self.assertRaises(RuntimeError) as ctx:
                    pipeline.analyze_video_mp4(
                        video, "key", "MiniMax-M3", duration=120.0, work_dir=root / "work"
                    )
                self.assertIn("rate limit exceeded", str(ctx.exception))
            self.assertEqual(compress.call_count, 1)
            self.assertEqual(upload.call_count, 1)



if __name__ == "__main__":
    unittest.main()
