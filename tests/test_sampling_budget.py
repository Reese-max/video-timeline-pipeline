import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline


class SamplingSelectionTests(unittest.TestCase):
    def test_scene_change_wins_within_its_window(self):
        probes = [{"timestamp": index, "scene_score": 0.0} for index in range(240)]
        probes[31]["scene_score"] = 0.95
        indexes = pipeline._select_budgeted_probe_indexes(probes, 10)
        self.assertEqual(len(indexes), 10)
        self.assertEqual(indexes[0], 0)
        self.assertEqual(indexes[-1], 239)
        self.assertIn(31, indexes)
        for window in range(8):
            self.assertTrue(any(239 * window / 8 <= probes[index]["timestamp"] < 239 * (window + 1) / 8 for index in indexes))

    def test_small_inputs_and_two_frame_budget(self):
        probes = [{"timestamp": index, "scene_score": 0.0} for index in range(5)]
        self.assertEqual(pipeline._select_budgeted_probe_indexes(probes, 40), list(range(5)))
        self.assertEqual(pipeline._select_budgeted_probe_indexes(probes, 2), [0, 4])

    def test_invalid_budget_rejected_before_process(self):
        with patch("pipeline.run_external") as process:
            for duration, maximum, candidates in ((0, 40, 240), (float("nan"), 40, 240), (100, 40, 39), (100, 0, 240)):
                with self.assertRaises(ValueError):
                    pipeline.extract_budgeted_candidates(Path("missing"), Path("unused"), duration, maximum, candidates)
        process.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for generated-media integration test")
class RealSamplingTests(unittest.TestCase):
    def test_two_hour_low_fps_source_stays_bounded_and_keeps_last_frame(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow required")
        with tempfile.TemporaryDirectory(prefix="long-sampling-test-") as temporary, patch.dict(os.environ, {"VIDEO_PROCESS_TIMEOUT_SECONDS": "180"}, clear=False):
            root = Path(temporary)
            source = root / "source.mp4"
            # Two hours of timestamps, only 720 generated source frames. No download.
            subprocess.run([
                shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=128x72:r=1/10:d=3600",
                "-f", "lavfi", "-i", "color=white:s=128x72:r=1/10:d=3600",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                "-fps_mode", "vfr", "-c:v", "mpeg4", str(source),
            ], check=True, timeout=60)
            rows, stats = pipeline.extract_budgeted_candidates(source, root / "job", 7200, 4, 240)
            self.assertLessEqual(stats["probe_count"], 240)
            self.assertEqual(len(rows), 4)
            self.assertTrue(stats["sparse_sampling"])
            self.assertGreaterEqual(rows[-1]["timestamp"], 7189.9)
            self.assertTrue(any(row["scene_score"] > 0.5 and 3600 <= row["timestamp"] < 3660 for row in rows), rows)
            self.assertEqual(len(list((root / "job" / "budgeted-frames").glob("*.jpg"))), 4)

    def test_real_scene_clip_budget_timestamps_and_cache(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow required")
        with tempfile.TemporaryDirectory(prefix="sampling-test-") as temporary, patch.dict(os.environ, {"VIDEO_PROCESS_TIMEOUT_SECONDS": "60"}, clear=False):
            root = Path(temporary)
            source = root / "source.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=160x90:r=10:d=4",
                "-f", "lavfi", "-i", "color=white:s=160x90:r=10:d=4",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                "-c:v", "mpeg4", str(source),
            ], check=True, timeout=30)
            rows, stats = pipeline.extract_budgeted_candidates(source, root / "job", 8, max_frames=4, candidate_budget=8)
            self.assertEqual(len(rows), 4)
            self.assertLessEqual(stats["probe_count"], 8)
            self.assertEqual(len(list((root / "job" / "budgeted-frames").glob("*.jpg"))), 4)
            self.assertLessEqual(rows[0]["timestamp"], 0.1)
            self.assertGreaterEqual(rows[-1]["timestamp"], 7.8)
            self.assertTrue(all(0 <= row["timestamp"] < 8 for row in rows))
            self.assertEqual([row["timestamp"] for row in rows], sorted(row["timestamp"] for row in rows))
            self.assertTrue(any(row["scene_score"] > 0.5 and 4 <= row["timestamp"] < 5.5 for row in rows))
            self.assertFalse(list((root / "job").glob("sampling-*")))
            with patch("pipeline.run_external", side_effect=AssertionError("cache miss")):
                cached, cached_stats = pipeline.extract_budgeted_candidates(source, root / "job", 8, 4, 8)
            self.assertEqual(cached, rows)
            self.assertEqual(cached_stats, stats)
            with patch("pipeline.run_external", side_effect=RuntimeError("changed config reached process")):
                with self.assertRaisesRegex(RuntimeError, "changed config"):
                    pipeline.extract_budgeted_candidates(source, root / "job", 8, 3, 8)


if __name__ == "__main__":
    unittest.main()
