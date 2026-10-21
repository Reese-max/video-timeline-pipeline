"""MCP_SEGMENT_MODE：預設直接剪開（stream copy），失敗才退回重新編碼。"""
import csv
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server as server
from execution_control import DeadlineExceeded, ExecutionCancelled

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def probe_entries(path: Path, entries: str) -> str:
    output = server.pipeline.run_external([
        server.pipeline.require_tool("ffprobe"), "-v", "error",
        "-select_streams", "v:0", "-show_entries", entries, "-of", "csv=p=0", str(path),
    ])
    return output.strip()


def probe_codec(path: Path) -> str:
    return probe_entries(path, "stream=codec_name").split(",")[0]


def probe_size(path: Path) -> tuple[int, int]:
    width, height = probe_entries(path, "stream=width,height").split(",")
    return int(width), int(height)


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
class SplitModeTests(unittest.TestCase):
    SEGMENT_SECONDS = 2

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.segments_dir = self.root / "segments"

    def env(self, **values):
        return patch.dict(os.environ, values)

    def make_source(self, name="source.mp4", size="64x64", duration=7, rate=7, gop=None,
                    video_args=None):
        path = self.root / name
        command = [
            server.pipeline.require_tool("ffmpeg"), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
        ]
        if gop:
            command += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
        command += list(video_args or ["-c:v", "libx264"])
        command += ["-c:a", "aac", str(path)]
        server.pipeline.run_external(command)
        return path

    def split(self, source: Path, duration: float | None = None,
              segment_seconds: int | None = None):
        if duration is None:
            duration = server.pipeline.video_duration(server.pipeline.probe_video(source))
        return server.split_video(source, self.segments_dir,
                                  segment_seconds or self.SEGMENT_SECONDS, duration)

    def segment_commands(self, calls) -> list[list[str]]:
        return [command for command in calls if "segment_%03d.mp4" in " ".join(command)]

    # 1. 預設 copy：維持來源編碼、指令含 -c copy、不含 -force_key_frames。
    def test_default_mode_copies_without_reencoding(self):
        source = self.make_source(gop=21)  # 每 3 秒一個關鍵畫面
        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        with self.env(MCP_SEGMENT_MODE="copy"), \
                patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = self.split(source)

        commands = self.segment_commands(calls)
        self.assertEqual(len(commands), 1, "copy 模式只跑一次 ffmpeg")
        command = commands[0]
        self.assertIn("-c", command)
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertNotIn("-force_key_frames", command)
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f") + 1], "segment")
        self.assertIn("-segment_time", command)

        source_codec = probe_codec(source)
        for path, _, _ in parts:
            self.assertEqual(probe_codec(path), source_codec, "分段必須維持來源編碼")
        self.assertGreater(len(parts), 1, "測試影片應切出多段")

        rows = list(csv.reader((self.segments_dir / "segments.csv")
                               .read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual(len(rows), len(parts))
        previous_end = 0.0
        for (path, start, end), row in zip(parts, rows, strict=True):
            self.assertEqual(path.name, row[0])
            self.assertGreaterEqual(start, previous_end - 0.001, "分段時間必須連續")
            self.assertGreater(end, start)
            previous_end = end
        self.assertAlmostEqual(parts[-1][2], self.split_duration(source), delta=0.5,
                               msg="最後一段必須接近影片長度")
        # copy 不能強制關鍵畫面：切點落在來源既有的關鍵畫面（3 秒），不是 2 秒。
        self.assertAlmostEqual(parts[1][1], 3.0, delta=0.15)

    def split_duration(self, source: Path) -> float:
        return server.pipeline.video_duration(server.pipeline.probe_video(source))

    def test_copy_keeps_non_h264_source_codec(self):
        for name, video_args, expected in (
            ("vp9", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"], "vp9"),
            ("mpeg4", ["-c:v", "mpeg4"], "mpeg4"),
        ):
            with self.subTest(codec=expected):
                try:
                    source = self.make_source(name=f"source_{name}.mp4", size="64x64",
                                              duration=4, gop=14, video_args=video_args)
                except RuntimeError:
                    if name == "mpeg4":
                        self.skipTest("本機 ffmpeg 沒有可用的非 H.264 編碼器")
                    continue
                with self.env(MCP_SEGMENT_MODE="copy"):
                    parts = self.split(source)
                self.assertEqual(probe_codec(source), expected)
                for path, _, _ in parts:
                    self.assertEqual(probe_codec(path), expected,
                                     "非 H.264 來源的分段必須維持原編碼")

    # 2. 退回：copy 失敗自動改跑 transcode；取消與逾時不得退回。
    def test_copy_failure_falls_back_to_transcode(self):
        source = self.make_source(size="640x360", duration=7, gop=21)
        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            if "-c" in command and command[command.index("-c") + 1] == "copy":
                raise RuntimeError("copy failed on purpose")
            return original(command, **kwargs)

        with self.env(MCP_SEGMENT_MODE="copy", MCP_SEGMENT_MAX_LONG_SIDE="320"), \
                patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = self.split(source)

        commands = self.segment_commands(calls)
        self.assertEqual(len(commands), 2, "copy 失敗後必須再跑一次重新編碼")
        self.assertIn("libx264", commands[1])
        for path, _, _ in parts:
            self.assertEqual(probe_codec(path), "h264")
            self.assertEqual(probe_size(path), (320, 180))

    def test_copy_validation_failure_falls_back_to_transcode(self):
        source = self.make_source(size="640x360", duration=7, gop=21)
        calls = []
        original = server.pipeline.run_external
        original_validate = server._validated_segments
        state = {"validated": 0}

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        def validate(segment_dir, duration):
            state["validated"] += 1
            if state["validated"] == 1:
                # 第一次是 copy 分段的驗證：模擬沒通過（列數／檔名／時間對不上）。
                raise RuntimeError("FFmpeg 影片分段與實際時間清單數量不一致。")
            return original_validate(segment_dir, duration)

        with self.env(MCP_SEGMENT_MODE="copy", MCP_SEGMENT_MAX_LONG_SIDE="320"), \
                patch.object(server.pipeline, "run_external", side_effect=spy), \
                patch.object(server, "_validated_segments", side_effect=validate):
            parts = self.split(source)

        commands = self.segment_commands(calls)
        self.assertEqual(len(commands), 2, "驗證失敗同樣必須退回重新編碼")
        self.assertIn("libx264", commands[1])
        for path, _, _ in parts:
            self.assertEqual(probe_codec(path), "h264")

    def test_cancellation_is_not_swallowed_by_the_fallback(self):
        source = self.make_source()
        for error in (ExecutionCancelled("cancelled"), DeadlineExceeded("timeout")):
            with self.subTest(error=type(error).__name__):
                calls = []
                original = server.pipeline.run_external

                def spy(command, **kwargs):
                    calls.append(command)
                    if "-c" in command and command[command.index("-c") + 1] == "copy":
                        raise error
                    return original(command, **kwargs)

                with self.env(MCP_SEGMENT_MODE="copy"), \
                        patch.object(server.pipeline, "run_external", side_effect=spy):
                    with self.assertRaises(type(error)):
                        self.split(source)
                self.assertEqual(len(self.segment_commands(calls)), 1,
                                 "取消或逾時時不得退回重新編碼")

    # 3. transcode 縮放規則。
    def test_transcode_scaling_rules(self):
        cases = [
            ("640x360", "320", (320, 180)),
            ("360x640", "320", (180, 320)),
            ("160x90", "320", (160, 90)),
            ("640x360", "0", (640, 360)),
        ]
        for size, limit, expected in cases:
            with self.subTest(size=size, limit=limit):
                source = self.make_source(name=f"src_{size}_{limit}.mp4", size=size, duration=3)
                with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_MAX_LONG_SIDE=limit):
                    parts = self.split(source)
                width, height = probe_size(parts[0][0])
                self.assertEqual((width, height), expected)
                self.assertEqual(probe_codec(parts[0][0]), "h264")

    # 4. 非法環境變數：ValueError 且不得呼叫 ffmpeg。
    def test_invalid_environment_values_fail_before_ffmpeg(self):
        source = self.make_source()
        cases = [
            {"MCP_SEGMENT_MODE": "abc"},
            {"MCP_SEGMENT_MODE": "reencode"},
            {"MCP_SEGMENT_MAX_LONG_SIDE": "-1"},
            {"MCP_SEGMENT_MAX_LONG_SIDE": "321"},
            {"MCP_SEGMENT_MAX_LONG_SIDE": "200"},
            {"MCP_SEGMENT_MAX_LONG_SIDE": "abc"},
            {"MCP_SEGMENT_X264_PRESET": "ultrafast"},
            {"MCP_SEGMENT_X264_PRESET": "nonsense"},
        ]
        for values in cases:
            with self.subTest(values=values):
                with self.env(**values), patch.object(
                        server.pipeline, "run_external",
                        side_effect=AssertionError("不應呼叫 ffmpeg")) as run:
                    with self.assertRaises(ValueError):
                        server.split_video(source, self.segments_dir, self.SEGMENT_SECONDS, 7.0)
                run.assert_not_called()

    # 5. 模式／設定改變必須全部重切；copy 模式不做續跑。
    def test_mode_change_forces_a_full_resplit(self):
        source = self.make_source()
        with self.env(MCP_SEGMENT_MODE="copy"):
            first = self.split(source)
        old = time.time() - 3600
        for path, _, _ in first:
            os.utime(path, (old, old))
        with self.env(MCP_SEGMENT_MODE="transcode"):
            second = self.split(source)
        self.assertTrue(second)
        for path, _, _ in second:
            self.assertGreater(path.stat().st_mtime, old + 60, "模式改變後全部分段都要重切")
        marker = json.loads((self.segments_dir / ".split-params.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["mode"], "transcode")

    def test_encoder_setting_change_forces_a_full_resplit(self):
        source = self.make_source()
        with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_X264_PRESET="veryfast"):
            first = self.split(source)
        old = time.time() - 3600
        for path, _, _ in first:
            os.utime(path, (old, old))
        with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_X264_PRESET="faster"):
            second = self.split(source)
        for path, _, _ in second:
            self.assertGreater(path.stat().st_mtime, old + 60, "preset 改變後全部分段都要重切")
        marker = json.loads((self.segments_dir / ".split-params.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["settings"]["x264_preset"], "faster")

    def test_copy_mode_never_resumes_and_clears_leftovers(self):
        source = self.make_source(gop=21)
        with self.env(MCP_SEGMENT_MODE="copy"):
            first = self.split(source)
        csv_path = self.segments_dir / "segments.csv"
        rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
        self.assertGreater(len(rows), 1)
        csv_path.write_text("".join(",".join(row) + "\n" for row in rows[:1]), encoding="utf-8")
        stray = self.segments_dir / "segment_099.mp4"
        stray.write_bytes(b"partial")
        kept = self.segments_dir / "segment_000.mp4"
        old = time.time() - 3600
        os.utime(kept, (old, old))

        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        with self.env(MCP_SEGMENT_MODE="copy"), \
                patch.object(server.pipeline, "run_external", side_effect=spy):
            second = self.split(source)

        self.assertFalse(stray.exists(), "中斷殘留的分段必須清掉")
        self.assertGreater(kept.stat().st_mtime, old + 60, "copy 模式不做續跑，已完成分段也要重切")
        self.assertEqual(len(second), len(first))
        commands = self.segment_commands(calls)
        self.assertEqual(len(commands), 1)
        self.assertNotIn("-ss", commands[0], "copy 模式不得用 -ss 續切")

    def test_marker_records_mode_and_settings(self):
        source = self.make_source()
        with self.env(MCP_SEGMENT_MODE="copy"):
            self.split(source)
        marker = json.loads((self.segments_dir / ".split-params.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["mode"], "copy")
        self.assertEqual(marker["segment_seconds"], self.SEGMENT_SECONDS)
        self.assertIn("copy", marker["encoder_args"])
        with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_MAX_LONG_SIDE="1280",
                      MCP_SEGMENT_X264_PRESET="faster"):
            self.split(source)
        marker = json.loads((self.segments_dir / ".split-params.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["mode"], "transcode")
        self.assertEqual(marker["settings"]["max_long_side"], 1280)
        self.assertEqual(marker["settings"]["x264_preset"], "faster")


if __name__ == "__main__":
    unittest.main()
