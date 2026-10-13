"""Owned offline regressions: status429 must not consume legitimate filesize digits."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline
from cost_tracker import BudgetBlockedError


class SizeStatusBoundaryTests(unittest.TestCase):
    def test_legitimate_large_byte_counts_with_429_digits_remain_size_rejections(self) -> None:
        for size in [642900000, 1429000000, 552429512]:
            with self.subTest(size=size):
                message = f"壓縮後仍有 {size} bytes，超過 MiniMax Files API 上限 512 MB。"
                self.assertTrue(pipeline.is_minimax_video_size_error(RuntimeError(message)))

    def test_standalone_numeric_rate_status_remains_non_size(self) -> None:
        for message in ["429", "API HTTP 429: rejected", "HTTP429 rejected", "status_code=429", "API HTTP 429: payload too large"]:
            with self.subTest(message=message):
                self.assertFalse(pipeline.is_minimax_video_size_error(RuntimeError(message)))

    def test_typed_rate_status_still_overrides_size_words(self) -> None:
        error = RuntimeError("file size limit exceeded: 642900000 bytes")
        error.status_code = 429
        self.assertFalse(pipeline.is_minimax_video_size_error(error))

    def test_existing_rate_budget_and_quota_exclusions_are_preserved(self) -> None:
        for error in [RuntimeError("rate limit exceeded"), RuntimeError("monthly quota exceeded"), RuntimeError("Budget blocked: projected exceeds daily limit"), BudgetBlockedError("payload too large", provider="minimax", operation="files.upload")]:
            with self.subTest(error=type(error).__name__, message=str(error)):
                self.assertFalse(pipeline.is_minimax_video_size_error(error))

    def test_real_oversize_branch_preserves_two_compressions_then_frames(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owned-video-size-boundary-") as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            compressed = root / "work" / "minimax-compressed.mp4"
            video.write_bytes(b"owned tiny mp4 fixture")
            original_stat = Path.stat

            def size_stat(self, *args, **kwargs):
                result = original_stat(self, *args, **kwargs)
                if self in [video, compressed]:
                    return mock.Mock(st_size=642900000, st_mtime_ns=result.st_mtime_ns, st_mode=result.st_mode)
                return result

            def compress(src, destination, **kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"owned compressed fixture")
                return destination

            observations = [{"timestamp": 0.0, "analysis_method": "minimax_video_fallback_frames", "fallback_reason": "owned size fixture"}]
            with mock.patch.object(Path, "stat", size_stat), mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=compress) as compressed_calls, mock.patch.object(pipeline, "minimax_upload_video_understanding") as uploads, mock.patch.object(pipeline, "minimax_chat") as chats, mock.patch.object(pipeline, "analyze_video_mp4_frames_fallback", return_value=observations) as frames:
                result = pipeline.analyze_video_mp4(video, "owned-synthetic-key", "MiniMax-M3", duration=120.0, work_dir=root / "work")
            self.assertEqual(result, observations)
            self.assertEqual(compressed_calls.call_count, 2)
            self.assertEqual([call.kwargs["target_bytes"] for call in compressed_calls.call_args_list], [pipeline.MINIMAX_VIDEO_COMPRESS_TARGET_BYTES, pipeline.MINIMAX_VIDEO_COMPRESS_RETRY_BYTES])
            uploads.assert_not_called()
            chats.assert_not_called()
            frames.assert_called_once()
            self.assertIn("642900000 bytes", frames.call_args.kwargs["fallback_reason"])
            self.assertIn("512 MB", frames.call_args.kwargs["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
