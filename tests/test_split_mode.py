"""MCP_SEGMENT_MODE：預設直接剪開（stream copy），失敗才退回重新編碼。"""
import csv
import json
import math
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server as server
from execution_control import DeadlineExceeded, ExecutionCancelled

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")

SCALE_PATTERN = re.compile(r"^scale='([^']*)':'([^']*)'$")


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


def probe_rotation(path: Path) -> int:
    """讀取顯示矩陣的旋轉角度；沒有旋轉資訊時回傳 0。"""
    output = server.pipeline.run_external([
        server.pipeline.require_tool("ffprobe"), "-v", "error",
        "-select_streams", "v:0", "-show_entries", "stream_side_data=rotation",
        "-of", "default=nw=1", str(path),
    ])
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("rotation="):
            return int(text.split("=", 1)[1])
    return 0


def split_arguments(text: str) -> list[str]:
    """依最外層的逗號切開運算式的參數（不切到巢狀括號內的逗號）。"""
    arguments, depth, current = [], 0, ""
    for character in text:
        if character == "," and depth == 0:
            arguments.append(current)
            current = ""
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        current += character
    arguments.append(current)
    return arguments


def translate_expression(text: str) -> str:
    """把 ffmpeg 的運算式語法翻譯成等價的 Python 運算式。"""
    text = text.strip()
    if text.startswith("if(") and text.endswith(")"):
        condition, yes, no = split_arguments(text[3:-1])
        return (f"({translate_expression(yes)} if {translate_expression(condition)}"
                f" else {translate_expression(no)})")
    if text.startswith("gt(") and text.endswith(")"):
        left, right = split_arguments(text[3:-1])
        return f"({translate_expression(left)} > {translate_expression(right)})"
    if text.startswith("min(") and text.endswith(")"):
        left, right = split_arguments(text[4:-1])
        return f"min({translate_expression(left)}, {translate_expression(right)})"
    # 其餘是「函式呼叫後接運算子」的形式，例如 trunc(min(320,iw)/2)*2。
    text = text.replace("gt(iw,ih)", "(iw > ih)")
    text = text.replace("trunc(", "int(")
    return text


def evaluate_scale(expression: str, iw: int, ih: int) -> tuple[int, int]:
    """把 ffmpeg 的 scale 運算式翻譯成 Python 求值，得到實際輸出的寬高。

    只支援 split_scale_filter 會產生的語法（if/gt/min/trunc/-2），目的是在測試裡
    直接檢查運算式本身，不必真的跑 ffmpeg；-2 代表依長寬比換算後取偶數倍數。
    """
    match = SCALE_PATTERN.match(expression.strip())
    assert match, f"無法解析縮放運算式：{expression}"
    width_expr, height_expr = match.groups()

    def evaluate(text: str) -> int:
        return int(eval(translate_expression(text), {"__builtins__": {}},  # 固定語法，無副作用
                         {"iw": iw, "ih": ih, "min": min, "int": int}))

    def even(value: float) -> int:
        # ffmpeg 的 -2 是「依長寬比換算後取最接近的偶數」，不是四捨五入到偶數。
        rounded = int(round(value))
        return rounded - rounded % 2

    width = evaluate(width_expr)
    height = evaluate(height_expr)
    if width == -2:
        width = even(iw * height / ih)
    else:
        height = even(ih * width / iw)
    return width, height


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
                    video_args=None, pix_fmt=None):
        path = self.root / name
        command = [
            server.pipeline.require_tool("ffmpeg"), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
        ]
        if gop:
            command += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
        command += list(video_args or ["-c:v", "libx264"])
        if pix_fmt:
            # yuv420p 的編碼器不接受奇數尺寸，奇數尺寸來源要用 4:4:4 才有機會產出。
            command += ["-pix_fmt", pix_fmt]
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
        self.assertAlmostEqual(parts[-1][2], self.split_duration(source), delta=0.15,
                               msg="最後一段必須接近影片長度")
        # copy 不能強制關鍵畫面：切點落在來源既有的關鍵畫面（3 秒），不是 2 秒。
        # copy 與 transcode 都用 -avoid_negative_ts disabled，分段沿用來源時間軸，
        # 切點與播放時間軸一致，不會整體平移。
        shift = parts[1][1] - 3.0
        self.assertAlmostEqual(parts[1][1], 3.0, delta=0.15)
        if len(parts) > 2:
            self.assertAlmostEqual(parts[2][1] - 6.0, shift, delta=0.01,
                                   msg="所有切點的偏移必須一致")

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
                    # 編碼器不可用要明確略過，不能靜靜 continue 讓測試在沒驗證時通過。
                    self.skipTest(f"本機 ffmpeg 沒有可用的 {expected} 編碼器")
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

        def validate(segment_dir, duration, *args, **kwargs):
            state["validated"] += 1
            if state["validated"] == 1:
                # 第一次是 copy 分段的驗證：模擬沒通過（列數／檔名／時間對不上）。
                raise RuntimeError("FFmpeg 影片分段與實際時間清單數量不一致。")
            return original_validate(segment_dir, duration, *args, **kwargs)

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


    def make_odd_source(self, size="171x95", duration=2, rate=7):
        """用 rawvideo 產生奇數尺寸來源。

        ffmpeg 的 scale 濾鏡會把奇數尺寸自動調成偶數，所以一般編碼路徑產生不出
        奇數尺寸的影片；rawvideo 直送 gray 影格不會經過 scale，才留得住奇數寬高。
        """
        width, height = (int(part) for part in size.split("x"))
        raw = self.root / f"raw_{size}.gray"
        with raw.open("wb") as handle:
            for _ in range(duration * rate):
                handle.write(bytes(width * height))
        path = self.root / f"odd_{size}.mkv"
        server.pipeline.run_external([
            server.pipeline.require_tool("ffmpeg"), "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", size, "-r", str(rate),
            "-i", str(raw), "-c:v", "ffv1", "-pix_fmt", "gray", str(path),
        ])
        return path

    # M1：縮放運算式必須把寬高都偶數化（libx264 的 yuv420p 需要偶數尺寸）。
    def test_scale_expression_always_yields_even_dimensions(self):
        for long_side in (320, 1280):
            for iw, ih in ((853, 480), (1279, 719), (171, 95), (95, 171), (160, 90)):
                with self.subTest(long_side=long_side, source=f"{iw}x{ih}"):
                    width, height = evaluate_scale(
                        server.split_scale_filter(long_side), iw, ih)
                    self.assertEqual(width % 2, 0, f"寬度必須是偶數：{width}")
                    self.assertEqual(height % 2, 0, f"高度必須是偶數：{height}")
                    self.assertLessEqual(width, iw, "只縮不放")
                    self.assertLessEqual(height, ih, "只縮不放")

    def test_transcode_odd_source_sizes_produce_even_dimensions(self):
        cases = [("171x95", "320", (170, 94)), ("853x480", "320", (320, 180)),
                 ("1279x719", "320", (320, 180))]
        for size, limit, expected in cases:
            with self.subTest(size=size):
                source = self.make_odd_source(size=size, duration=2)
                source_width, source_height = probe_size(source)
                self.assertTrue(source_width % 2 or source_height % 2,
                                "測試來源必須是奇數尺寸")
                with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_MAX_LONG_SIDE=limit):
                    parts = self.split(source)
                width, height = probe_size(parts[0][0])
                self.assertEqual((width, height), expected)
                self.assertEqual(width % 2, 0)
                self.assertEqual(height % 2, 0)

    # X2：MCP_SEGMENT_MAX_LONG_SIDE=0 不縮放，仍要把奇數寬高偶數化（libx264 yuv420p）；
    # 偶數來源尺寸不變。
    def test_no_scaling_still_evenizes_odd_dimensions(self):
        source = self.make_odd_source(size="171x95", duration=2)
        source_width, source_height = probe_size(source)
        self.assertTrue(source_width % 2 or source_height % 2,
                        "測試來源必須是奇數尺寸")
        with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_MAX_LONG_SIDE="0"):
            parts = self.split(source)
        width, height = probe_size(parts[0][0])
        self.assertEqual(width % 2, 0, "不縮放模式輸出寬度仍必須是偶數")
        self.assertEqual(height % 2, 0, "不縮放模式輸出高度仍必須是偶數")
        self.assertEqual((width, height), (170, 94))
        even_source = self.make_source(name="even_640x360.mp4", size="640x360", duration=2)
        with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_MAX_LONG_SIDE="0"):
            parts = self.split(even_source)
        self.assertEqual(probe_size(parts[0][0]), (640, 360),
                         "偶數來源在不縮放模式尺寸不得改變")

    # M2：copy 的分段驗證階段拋 ValueError 時也必須退回重新編碼。
    def test_copy_validation_value_error_falls_back_to_transcode(self):
        source = self.make_source(size="640x360", duration=7, gop=21)
        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            result = original(command, **kwargs)
            if "-c" in command and command[command.index("-c") + 1] == "copy":
                # 破壞 copy 的分段清單：時間欄位不是數字 → 驗證階段會拋 ValueError。
                csv_path = self.segments_dir / "segments.csv"
                rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
                self.assertTrue(rows)
                rows[0][1] = "not-a-number"
                rows[0][2] = "also-not-a-number"
                csv_path.write_text("".join(",".join(row) + "\n" for row in rows),
                                    encoding="utf-8")
            return result

        with self.env(MCP_SEGMENT_MODE="copy", MCP_SEGMENT_MAX_LONG_SIDE="320"), \
                patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = self.split(source)

        commands = self.segment_commands(calls)
        self.assertEqual(len(commands), 2, "驗證階段的 ValueError 必須退回重新編碼")
        self.assertIn("libx264", commands[1])
        for path, _, _ in parts:
            self.assertEqual(probe_codec(path), "h264")
            self.assertEqual(probe_size(path), (320, 180))
        marker = json.loads((self.segments_dir / ".split-params.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["mode"], "transcode")

    # L1：只有 ffmpeg 執行失敗與分段驗證失敗才退回；前置／收尾錯誤直接往上拋。
    def test_pre_split_errors_are_not_swallowed_by_the_fallback(self):
        source = self.make_source(size="640x360", duration=7, gop=21)
        duration = self.split_duration(source)
        cases = [
            ("file_fingerprint", "file_fingerprint", OSError("fingerprint failed")),
            ("marker_write", "write_json", OSError("marker write failed")),
        ]
        for name, target, error in cases:
            with self.subTest(failure=name):
                calls = []
                original = server.pipeline.run_external

                def spy(command, **kwargs):
                    calls.append(command)
                    return original(command, **kwargs)

                # 只讓第一次呼叫失敗：原本的退回邏輯會接著跑重新編碼並成功，
                # 因此這個測試在修正前會看到第二次 ffmpeg。
                with self.env(MCP_SEGMENT_MODE="copy"), \
                        patch.object(server.pipeline, "run_external", side_effect=spy), \
                        patch.object(server.pipeline, target, side_effect=[error, "ok"]):
                    with self.assertRaises(OSError):
                        server.split_video(source, self.segments_dir,
                                           self.SEGMENT_SECONDS, duration)
                self.assertEqual(self.segment_commands(calls), [],
                                 "前置錯誤不得退回重新編碼")

    # L2：清掉分段檔時要容忍檔案已經不存在。
    def test_clear_split_artifacts_tolerates_missing_files(self):
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        (self.segments_dir / "segment_000.mp4").write_bytes(b"partial")
        (self.segments_dir / "segments.csv").write_text("a,b,c\n", encoding="utf-8")
        real_glob = Path.glob

        def glob(path, pattern):
            found = list(real_glob(path, pattern))
            for stale in found:
                # 模擬檔案在列出之後、刪除之前就被移走（退回時的清理不該因此失敗）。
                stale.unlink(missing_ok=True)
            return iter(found)

        with patch.object(Path, "glob", autospec=True, side_effect=glob):
            server._clear_split_artifacts(self.segments_dir)
        self.assertFalse((self.segments_dir / "segments.csv").exists())

    # L3：copy 與 transcode 都用 disabled；make_zero 會把切點時間整體平移。
    def test_avoid_negative_ts_disabled_in_both_modes(self):
        source = self.make_source(size="64x64", duration=5, gop=7)
        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        for mode in ("copy", "transcode"):
            with self.subTest(mode=mode):
                calls.clear()
                with self.env(MCP_SEGMENT_MODE=mode), \
                        patch.object(server.pipeline, "run_external", side_effect=spy):
                    self.split(source)
                command = self.segment_commands(calls)[0]
                self.assertIn("-avoid_negative_ts", command)
                self.assertEqual(command[command.index("-avoid_negative_ts") + 1], "disabled")

    # L5：copy 與 transcode 的指令都要明確指定影像與音軌。
    def test_both_modes_map_video_and_optional_audio(self):
        source = self.make_source(size="64x64", duration=5, gop=7)
        calls = []
        original = server.pipeline.run_external

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        for mode in ("copy", "transcode"):
            with self.subTest(mode=mode):
                calls.clear()
                with self.env(MCP_SEGMENT_MODE=mode), \
                        patch.object(server.pipeline, "run_external", side_effect=spy):
                    self.split(source)
                command = self.segment_commands(calls)[0]
                self.assertIn("0:v:0", command)
                self.assertIn("0:a:0?", command)
                self.assertEqual(command[command.index("-map") + 1], "0:v:0")

    # L5：copy 真的切出分段後才失敗時，退回必須清掉半成品並留下 transcode 設定。
    def test_fallback_replaces_copy_segments_and_records_transcode_marker(self):
        try:
            source = self.make_source(size="640x360", duration=7, gop=21,
                                      video_args=["-c:v", "mpeg4"])
        except RuntimeError:
            self.skipTest("本機 ffmpeg 沒有可用的 mpeg4 編碼器")
        calls = []
        original = server.pipeline.run_external
        copy_names: list[str] = []

        def spy(command, **kwargs):
            calls.append(command)
            result = original(command, **kwargs)
            if "-c" in command and command[command.index("-c") + 1] == "copy":
                # copy 真的切出了分段，接著才失敗（模擬驗證或寫檔階段出錯）。
                copy_names.extend(sorted(p.name for p in self.segments_dir.glob("segment_*.mp4")))
                csv_path = self.segments_dir / "segments.csv"
                rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
                if len(rows) > 1:
                    csv_path.write_text("".join(",".join(row) + "\n" for row in rows[:1]),
                                        encoding="utf-8")
                (self.segments_dir / "segment_099.mp4").write_bytes(b"partial")
            return result

        with self.env(MCP_SEGMENT_MODE="copy", MCP_SEGMENT_MAX_LONG_SIDE="320"), \
                patch.object(server.pipeline, "run_external", side_effect=spy):
            parts = self.split(source)

        commands = self.segment_commands(calls)
        self.assertEqual(len(commands), 2, "copy 失敗後必須再跑一次重新編碼")
        self.assertIn("libx264", commands[1])
        self.assertTrue(copy_names, "copy 必須真的產生分段")

        self.assertFalse((self.segments_dir / "segment_099.mp4").exists(),
                         "copy 的半成品必須清掉")
        listed = [row[0] for row in csv.reader(
            (self.segments_dir / "segments.csv").read_text(encoding="utf-8-sig").splitlines())]
        on_disk = sorted(p.name for p in self.segments_dir.glob("segment_*.mp4"))
        self.assertEqual(sorted(listed), on_disk, "目錄裡不得留有沒列入清單的分段")
        for path, _, _ in parts:
            self.assertEqual(probe_codec(path), "h264", "退回後的分段必須是 H.264")
            self.assertEqual(probe_size(path), (320, 180))
        marker = json.loads((self.segments_dir / ".split-params.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["mode"], "transcode")
        self.assertIn("libx264", marker["encoder_args"])

    # L5：帶旋轉 metadata 的來源，縮放後必須是直式且寬高皆偶數。
    def test_rotated_source_is_scaled_as_portrait(self):
        plain = self.make_source(name="plain_rot.mp4", size="640x360", duration=3)
        rotated = self.root / "rotated.mp4"
        try:
            server.pipeline.run_external([
                server.pipeline.require_tool("ffmpeg"), "-y", "-v", "error",
                "-display_rotation", "90", "-i", str(plain),
                "-c", "copy", str(rotated)])
        except RuntimeError as error:
            self.skipTest(f"本機 ffmpeg 不支援 -display_rotation：{error}")
        if not probe_rotation(rotated):
            self.skipTest("本機 ffmpeg 無法產生帶旋轉 metadata 的來源")
        with self.env(MCP_SEGMENT_MODE="transcode", MCP_SEGMENT_MAX_LONG_SIDE="320"):
            parts = self.split(rotated)
        width, height = probe_size(parts[0][0])
        self.assertGreater(height, width, "旋轉後的來源必須縮成直式")
        self.assertEqual(max(width, height), 320)
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)


if __name__ == "__main__":
    unittest.main()
