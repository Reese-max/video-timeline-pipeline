"""split_video 的切割進度回報與中斷續切。"""
import csv
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server as server

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
class SplitProgressResumeTests(unittest.TestCase):
    SEGMENT_SECONDS = 2

    def setUp(self):
        # 續切只適用重新編碼模式：copy 模式重切只要幾秒，不做續跑。
        self.env = patch.dict(os.environ, {"MCP_SEGMENT_MODE": "transcode"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.mp4"
        server.pipeline.run_external([
            server.pipeline.require_tool("ffmpeg"), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=7:duration=7",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=7",
            "-c:v", "libx264", "-c:a", "aac", str(self.source)])
        self.duration = server.pipeline.video_duration(server.pipeline.probe_video(self.source))
        self.assertGreater(self.duration, 0)
        self.segments_dir = self.root / "segments"

    def split(self):
        return server.split_video(self.source, self.segments_dir, self.SEGMENT_SECONDS, self.duration)

    def test_progress_reports_counts_and_stops_with_split(self):
        calls = []
        with patch.object(server, "report_progress", side_effect=lambda *args: calls.append(args)):
            parts = self.split()
        self.assertTrue(parts)
        split_calls = [call for call in calls if call[0] == "split"]
        self.assertTrue(split_calls, "切割期間至少要回報一次進度")
        for _, current, _ in split_calls:
            self.assertGreaterEqual(current, 45)
            self.assertLessEqual(current, 64)
        self.assertTrue(any(re.search(r"切割影片分段 \d+/\d+", call[2]) for call in split_calls),
                        "進度訊息應包含已完成段數/總段數")
        finished = len(calls)
        time.sleep(0.5)
        self.assertEqual(len(calls), finished, "split 結束後不得再有新的進度回報")

    def test_resume_keeps_finished_segments_and_merges_timelines(self):
        baseline = self.split()
        baseline_times = [(start, end) for _, start, end in baseline]
        csv_path = self.segments_dir / "segments.csv"
        rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
        self.assertGreater(len(rows), 2)
        # 模擬中斷：csv 只留前兩列，並放一個未列入 csv 的半成品分段。
        csv_path.write_text("".join(",".join(row) + "\n" for row in rows[:2]), encoding="utf-8")
        half = self.segments_dir / "segment_099.mp4"
        half.write_bytes(b"partial")
        kept = [self.segments_dir / "segment_000.mp4", self.segments_dir / "segment_001.mp4"]
        mtimes = [path.stat().st_mtime_ns for path in kept]

        reports = []
        with patch.object(server, "report_progress", side_effect=lambda *args: reports.append(args)):
            parts = self.split()

        self.assertFalse(half.exists(), "未列入 csv 的半成品分段必須刪除")
        self.assertEqual([path.stat().st_mtime_ns for path in kept], mtimes,
                         "已完成的分段不應被重編")
        self.assertEqual(len(parts), len(baseline_times))
        self.assertEqual([path.name for path, _, _ in parts],
                         [f"segment_{index:03d}.mp4" for index in range(len(parts))])
        for (_, start, end), (base_start, base_end) in zip(parts, baseline_times):
            self.assertAlmostEqual(start, base_start, delta=0.05)
            self.assertAlmostEqual(end, base_end, delta=0.05)
        progress_messages = [call[2] for call in reports if call[0] == "split"]
        self.assertFalse(any(" 0/" in message or " 1/" in message for message in progress_messages),
                         "續切進度必須從已完成段數開始顯示")

    def test_changed_encoder_args_force_a_full_resplit(self):
        baseline = self.split()
        files = [path for path, _, _ in baseline]
        old = time.time() - 3600
        for path in files:
            os.utime(path, (old, old))
        marker_path = self.segments_dir / ".split-params.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["encoder_args"] = [*marker["encoder_args"], "-x"]
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        with patch.object(server.pipeline, "run_external", side_effect=spy):
            rerun = self.split()
        self.assertTrue(calls, "參數標記與目前不一致時必須重跑 ffmpeg")
        self.assertEqual(len(rerun), len(baseline))
        for path, _, _ in rerun:
            self.assertGreater(path.stat().st_mtime, old + 60, "參數變更後全部分段都要重切")

    def test_completed_segments_do_not_run_ffmpeg(self):
        baseline = self.split()
        with patch.object(server.pipeline, "run_external",
                          side_effect=AssertionError("不應呼叫 ffmpeg")) as run:
            parts = self.split()
        run.assert_not_called()
        self.assertEqual([(path.name, start, end) for path, start, end in parts],
                         [(path.name, start, end) for path, start, end in baseline])


if __name__ == "__main__":
    unittest.main()
