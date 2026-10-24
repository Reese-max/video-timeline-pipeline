"""Frame sampling was removed: video is only ever analysed by MiniMax reading the whole MP4.

A truncated whole-video answer (finish_reason=length) stays incomplete instead of
switching to sampled images, and frame-derived results are never reused.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import pipeline


class VideoFramesRemovedTests(unittest.TestCase):
    def test_frame_sampling_entry_points_are_gone(self):
        for name in ("analyze_video_mp4_frames_fallback", "extract_budgeted_candidates", "analyze_images"):
            self.assertFalse(hasattr(pipeline, name), name)

    def test_frames_mode_is_rejected(self):
        for raw in ("frames", "frame", "images", "image"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"ANALYZE_VISUAL_MODE": raw}):
                with self.assertRaises(RuntimeError) as ctx:
                    pipeline.visual_mode()
                self.assertIn("已移除", str(ctx.exception))

    def test_truncated_response_stays_incomplete_and_uncached(self):
        with tempfile.TemporaryDirectory(prefix="video-trunc-") as temporary:
            root = Path(temporary)
            video = root / "owned.mp4"
            video.write_bytes(b"owned synthetic media; provider is stubbed")
            truncated = pipeline.MiniMaxIncompleteError("length", "MiniMax-M3")
            with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="owned-file") as upload, \
                 mock.patch.object(pipeline, "minimax_chat", side_effect=truncated) as chat, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress:
                observations = pipeline.analyze_video_mp4(
                    video, "owned-not-a-credential", "MiniMax-M3", duration=552.4,
                    work_dir=root / "work", cache_dir=root / "cache")
                cache_files = list((root / "cache").glob("*.json"))
        self.assertEqual((upload.call_count, chat.call_count, compress.call_count), (1, 1, 0))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["completion_status"], "incomplete")
        self.assertEqual(observations[0]["finish_reason"], "length")
        self.assertEqual(observations[0]["analysis_method"], "minimax_video")
        self.assertEqual(cache_files, [])

    def test_truncated_compressed_response_stays_incomplete(self):
        def fake_compress(_source, output, **_kwargs):
            Path(output).write_bytes(b"compressed")

        with tempfile.TemporaryDirectory(prefix="video-trunc-") as temporary:
            root = Path(temporary)
            video = root / "owned.mp4"
            video.write_bytes(b"owned synthetic media")
            truncated = pipeline.MiniMaxIncompleteError("length", "MiniMax-M3")
            with mock.patch.object(pipeline, "choose_video_mp4_upload_strategy", return_value="compress_first"), \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=fake_compress), \
                 mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="f"), \
                 mock.patch.object(pipeline, "minimax_chat", side_effect=truncated) as chat:
                observations = pipeline.analyze_video_mp4(
                    video, "owned-not-a-credential", "MiniMax-M3", duration=60.0, work_dir=root / "work")
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["completion_status"], "incomplete")
        self.assertEqual(observations[0]["analysis_method"], "minimax_video_compressed")

    def test_frame_derived_results_are_never_reused(self):
        event = {
            "timestamp": 90.0, "screen_type": "畫面", "visible_text": [], "visual_summary": "摘要",
            "important_details": [], "confidence": 0.9, "completion_status": "complete",
            "visual_schema_version": pipeline.VISUAL_OBSERVATION_SCHEMA_VERSION,
            "timestamp_original": 90.0, "timestamp_reliable": True,
            "video_gap_check_version": pipeline.VIDEO_EVENT_GAP_CHECK_VERSION,
            "unanswered_gap_windows": [],
        }
        self.assertTrue(pipeline.reusable_video_artifact([{**event, "analysis_method": "minimax_video"}]))
        for method in ("minimax_video_fallback_frames", "minimax", "frames"):
            with self.subTest(method=method):
                self.assertFalse(pipeline.reusable_video_artifact([{**event, "analysis_method": method}]))


if __name__ == "__main__":
    unittest.main()
