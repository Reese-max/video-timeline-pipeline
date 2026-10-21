"""split_video 的切割進度回報與中斷續切。"""
import csv
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server as server
from execution_control import DeadlineExceeded, ExecutionCancelled

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
        saw_split_report = threading.Event()
        original = server.pipeline.run_external

        def report(*args):
            calls.append(args)
            if args[0] == "split":
                saw_split_report.set()

        def spy(command, **kwargs):
            if "segment_%03d.mp4" in " ".join(str(part) for part in command):
                # 等到切割進度執行緒在 ffmpeg 執行期間至少回報一次才放行，
                # 避免只靠 split 結束後的最終回報就滿足「至少一次」的斷言。
                self.assertTrue(saw_split_report.wait(5.0),
                                "ffmpeg 執行期間必須已有切割進度回報")
            return original(command, **kwargs)

        with patch.object(server, "SPLIT_PROGRESS_POLL_SECONDS", 0.05), \
                patch.object(server, "report_progress", side_effect=report), \
                patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = self.split()
            finished = len(calls)
            # 等數倍輪詢間隔：若進度執行緒沒有停止，這段時間內會再冒出回報。
            time.sleep(server.SPLIT_PROGRESS_POLL_SECONDS * 6)
            self.assertEqual(len(calls), finished, "split 結束後不得再有新的進度回報")
        self.assertTrue(parts)
        split_calls = [call for call in calls if call[0] == "split"]
        self.assertTrue(split_calls, "切割期間至少要回報一次進度")
        for _, current, _ in split_calls:
            self.assertGreaterEqual(current, 45)
            self.assertLessEqual(current, 64)
        self.assertTrue(any(re.search(r"切割影片分段 \d+/\d+", call[2]) for call in split_calls),
                        "進度訊息應包含已完成段數/總段數")

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

    # H1：尾段缺失超過 0.25 秒就必須續切，不能用 1 秒容差誤判全部完成。
    def test_missing_tail_segment_resumes_instead_of_passing(self):
        baseline = self.split()
        csv_path = self.segments_dir / "segments.csv"
        rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
        self.assertGreater(len(rows), 3)
        # 模擬切到 6 秒就中斷：csv 只留前三段（0–6 秒），最後一段不存在；
        # 舊的 1.0 秒容差會把 6 >= 7-1 誤判成全部完成而跳過 ffmpeg。
        csv_path.write_text("".join(",".join(row) + "\n" for row in rows[:3]),
                            encoding="utf-8")

        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        with patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = self.split()

        segment_calls = [command for command in calls
                         if "segment_%03d.mp4" in " ".join(str(part) for part in command)]
        self.assertTrue(segment_calls, "缺少尾段時必須呼叫 ffmpeg 續切")
        self.assertEqual(len(parts), len(baseline), "續切後列數必須與基準一致")
        self.assertAlmostEqual(parts[-1][2], self.duration, delta=0.25,
                               msg="續切後最後一段必須接近影片長度")

    # H1：duration 恰為 segment_seconds 整數倍、全部完成時不得再要求不存在的尾段。
    def test_exact_multiple_duration_does_not_resume(self):
        source = self.root / "six_seconds.mp4"
        server.pipeline.run_external([
            server.pipeline.require_tool("ffmpeg"), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=7:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=6",
            "-c:v", "libx264", "-c:a", "aac", str(source)])
        duration = server.pipeline.video_duration(server.pipeline.probe_video(source))
        segments_dir = self.root / "segments_six"
        baseline = server.split_video(source, segments_dir, self.SEGMENT_SECONDS, duration)
        with patch.object(server.pipeline, "run_external",
                          side_effect=AssertionError("不應呼叫 ffmpeg")) as run:
            parts = server.split_video(source, segments_dir, self.SEGMENT_SECONDS, duration)
        run.assert_not_called()
        self.assertEqual(len(parts), len(baseline))

    # L1：不傳 duration 時自行 probe；已全部完成就不再呼叫 ffmpeg。
    def test_duration_none_probes_instead_of_rerunning(self):
        baseline = self.split()
        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        with patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = server.split_video(self.source, self.segments_dir,
                                       self.SEGMENT_SECONDS, None)
        segment_calls = [command for command in calls
                         if "segment_%03d.mp4" in " ".join(str(part) for part in command)]
        self.assertEqual(segment_calls, [],
                         "duration=None 時 probe 到長度、已全部完成就不應重跑 ffmpeg")
        self.assertTrue(any("ffprobe" in Path(str(command[0])).name for command in calls),
                        "duration=None 時必須用 probe 取得影片長度")
        self.assertEqual([(path.name, start, end) for path, start, end in parts],
                         [(path.name, start, end) for path, start, end in baseline])

    # L3：取消與逾時往上拋之後，進度執行緒必須已經停止、不得再回報。
    def test_progress_thread_stops_when_split_is_cancelled(self):
        for error in (ExecutionCancelled("cancelled"), DeadlineExceeded("timeout")):
            with self.subTest(error=type(error).__name__):
                segments_dir = self.root / f"segments_{type(error).__name__}"
                calls = []

                def spy(command, **kwargs):
                    raise error

                with patch.object(server, "SPLIT_PROGRESS_POLL_SECONDS", 0.05), \
                        patch.object(server, "report_progress",
                                     side_effect=lambda *args: calls.append(args)), \
                        patch.object(server.pipeline, "run_external", side_effect=spy):
                    with self.assertRaises(type(error)):
                        server.split_video(self.source, segments_dir,
                                           self.SEGMENT_SECONDS, self.duration)
                    finished = len(calls)
                    # 等數倍輪詢間隔：執行緒若還活著會繼續回報。
                    time.sleep(server.SPLIT_PROGRESS_POLL_SECONDS * 6)
                    self.assertEqual(len(calls), finished,
                                     "取消或逾時後不得再有新的進度回報")
                self.assertFalse(
                    any(thread.name == "split-progress" and thread.is_alive()
                        for thread in threading.enumerate()),
                    "split 結束後進度執行緒必須已停止")


if __name__ == "__main__":
    unittest.main()
