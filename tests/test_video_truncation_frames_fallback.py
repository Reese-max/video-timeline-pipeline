"""A truncated whole-video MiniMax answer (finish_reason=length) falls back to frame sampling."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import pipeline


class VideoTruncationFramesFallbackTests(unittest.TestCase):
    def run_truncated(self, **kwargs):
        with tempfile.TemporaryDirectory(prefix="video-trunc-") as temporary:
            root = Path(temporary)
            video = root / "owned.mp4"
            video.write_bytes(b"owned synthetic media; provider is stubbed")
            fallback_rows = [{"timestamp": 3.0, "visual_summary": "frame", "completion_status": "complete"}]
            truncated = pipeline.MiniMaxIncompleteError("length", "MiniMax-M3")
            with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="owned-file") as upload, \
                 mock.patch.object(pipeline, "minimax_chat", side_effect=truncated) as chat, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress, \
                 mock.patch.object(pipeline, "analyze_video_mp4_frames_fallback", return_value=fallback_rows) as frames:
                observations = pipeline.analyze_video_mp4(
                    video, "owned-not-a-credential", "MiniMax-M3", duration=552.4,
                    work_dir=root / "work", cache_dir=root / "cache", **kwargs)
                cache_files = list((root / "cache").glob("*.json"))
                return observations, upload.call_count, chat.call_count, compress.call_count, frames, cache_files

    def test_truncated_response_uses_frames_fallback(self):
        observations, uploads, chats, compression, frames, cache = self.run_truncated()
        self.assertEqual(observations[0]["visual_summary"], "frame")
        self.assertEqual((uploads, chats, compression, frames.call_count), (1, 1, 0, 1))
        self.assertIn("finish_reason=length", frames.call_args.kwargs["fallback_reason"])
        self.assertEqual(cache, [])

    def test_truncated_compressed_response_uses_frames_fallback_once(self):
        def fake_compress(_source, output, **_kwargs):
            Path(output).write_bytes(b"compressed")

        with mock.patch.object(pipeline, "choose_video_mp4_upload_strategy", return_value="compress_first"), \
             mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=fake_compress):
            with tempfile.TemporaryDirectory(prefix="video-trunc-") as temporary:
                root = Path(temporary)
                video = root / "owned.mp4"
                video.write_bytes(b"owned synthetic media")
                truncated = pipeline.MiniMaxIncompleteError("length", "MiniMax-M3")
                with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="f"), \
                     mock.patch.object(pipeline, "minimax_chat", side_effect=truncated) as chat, \
                     mock.patch.object(pipeline, "analyze_video_mp4_frames_fallback", return_value=[{"timestamp": 1.0}]) as frames:
                    observations = pipeline.analyze_video_mp4(
                        video, "owned-not-a-credential", "MiniMax-M3", duration=60.0, work_dir=root / "work")
        self.assertEqual(observations, [{"timestamp": 1.0}])
        self.assertEqual((chat.call_count, frames.call_count), (1, 1))

    def test_fallback_exception_propagates(self):
        for error in (RuntimeError("frames failed"), pipeline.DeadlineExceeded("deadline")):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(pipeline, "analyze_video_mp4_frames_fallback", side_effect=error), \
                 tempfile.TemporaryDirectory(prefix="video-trunc-") as temporary:
                root = Path(temporary)
                video = root / "owned.mp4"
                video.write_bytes(b"owned synthetic media")
                truncated = pipeline.MiniMaxIncompleteError("length", "MiniMax-M3")
                with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="f"), \
                     mock.patch.object(pipeline, "minimax_chat", side_effect=truncated), \
                     mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress, \
                     self.assertRaises(type(error)):
                    pipeline.analyze_video_mp4(
                        video, "owned-not-a-credential", "MiniMax-M3", duration=60.0, work_dir=root / "work")
                compress.assert_not_called()

    def test_truncated_response_stays_incomplete_when_fallback_disabled(self):
        observations, _, _, _, frames, cache = self.run_truncated(allow_frames_fallback=False)
        self.assertEqual(frames.call_count, 0)
        self.assertEqual(observations[0]["completion_status"], "incomplete")
        self.assertEqual(observations[0]["finish_reason"], "length")
        self.assertEqual(cache, [])


if __name__ == "__main__":
    unittest.main()
