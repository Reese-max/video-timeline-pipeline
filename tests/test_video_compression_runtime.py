"""Issue #76: exercise real MP4 muxing and the local compression failure boundary."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class CompressionRuntimeTests(unittest.TestCase):
    def test_real_short_mp4_compresses_and_remains_readable(self):
        self.assertIsNotNone(shutil.which("ffmpeg"), "FFmpeg is required by the offline gate")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:size=160x120:rate=10:duration=0.4",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True)
            destination = root / "work" / "compressed.mp4"
            result = pipeline.compress_video_for_minimax_upload(source, destination, duration=1.0)
            self.assertEqual(result, destination)
            self.assertGreater(destination.stat().st_size, 0)
            self.assertGreater(pipeline.video_duration(pipeline.probe_video(destination)), 0)
            self.assertEqual(list(destination.parent.glob("*.partial*")), [])

    def test_short_clip_encoder_rates_fit_signed_int(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "output.mp4"
            captured = []

            def encode(command, **kwargs):
                captured.extend(command)
                Path(command[-1]).write_bytes(b"encoded")

            with mock.patch.object(pipeline, "require_tool", return_value="ffmpeg"), \
                 mock.patch.object(pipeline, "run_external", side_effect=encode):
                pipeline.compress_video_for_minimax_upload(Path(raw) / "source.mp4", output, duration=1.0)
            for flag in ("-b:v", "-maxrate", "-bufsize"):
                value = captured[captured.index(flag) + 1]
                self.assertLessEqual(int(value.removesuffix("k")) * 1000, 2**31 - 1)

    def test_generic_compression_failure_raises_without_frames(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mp4"
            source.write_bytes(b"fixture")
            with mock.patch.object(pipeline, "choose_video_mp4_upload_strategy", return_value="compress_first"), \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload", side_effect=RuntimeError("Unable to choose an output format")) as compress, \
                 mock.patch.object(pipeline, "minimax_upload_video_understanding") as upload:
                with self.assertRaises(RuntimeError) as ctx:
                    pipeline.analyze_video_mp4(source, "synthetic-key", "MiniMax-M3", duration=1.0, work_dir=root / "work")
            self.assertIn("抽幀備援已移除", str(ctx.exception))
            self.assertEqual(compress.call_count, 2)
            upload.assert_not_called()

    def test_failed_encoder_cleans_partial_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "output.mp4"
            output.write_bytes(b"previous output")

            def fail(command, **kwargs):
                Path(command[-1]).write_bytes(b"partial bytes")
                raise RuntimeError("encoder failed")

            with mock.patch.object(pipeline, "require_tool", return_value="ffmpeg"), \
                 mock.patch.object(pipeline, "run_external", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                    pipeline.compress_video_for_minimax_upload(root / "source.mp4", output, duration=1.0)
            self.assertEqual(output.read_bytes(), b"previous output")
            self.assertEqual(list(root.glob("*.partial*")), [])
