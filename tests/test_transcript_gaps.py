"""issue #55：Whisper 在切段邊界漏轉（120.0–149.6s 約 30 秒空白）。

驗收：0–166 秒逐字稿不得有超過 5 秒空白（片頭片尾除外）。
策略：合併切段逐字稿後偵測內部斷層，對斷層補轉（前後各取
15 秒上下文），再合併回來。以下測試先寫、實作後補。
"""

import json
import unittest
from pathlib import Path


def _issue55_segments() -> list[dict]:
    """重現 job 16fae7864ca8：117.9–120.0 後直接跳到 149.6。"""
    return [
        {"start": 100.0, "end": 117.9, "text": "会自动归类到费用这个页面里面去", "chunk": 0},
        {"start": 117.9, "end": 120.0, "text": "而且还能看到逐年的趋势", "chunk": 0},
        {"start": 149.6, "end": 155.0, "text": "就像这个工作台", "chunk": 1},
        {"start": 155.0, "end": 166.0, "text": "最后能变成生活里用得上的东西", "chunk": 1},
    ]


class FindTranscriptGapsTest(unittest.TestCase):
    def test_detects_issue55_hole(self) -> None:
        from pipeline import find_transcript_gaps

        gaps = find_transcript_gaps(_issue55_segments())
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0]["start"], 120.0)
        self.assertAlmostEqual(gaps[0]["end"], 149.6)

    def test_ignores_head_tail_and_small_gaps(self) -> None:
        from pipeline import find_transcript_gaps

        segments = [
            {"start": 8.0, "end": 10.0, "text": "片頭後才開始", "chunk": 0},
            {"start": 12.0, "end": 20.0, "text": "2 秒空白不算斷層", "chunk": 0},
            {"start": 25.0, "end": 150.0, "text": "5 秒整不算斷層", "chunk": 1},
            {"start": 150.0, "end": 160.0, "text": "片尾前結束", "chunk": 1},
        ]
        # 開頭 0–8 與結尾 160–166 是片頭片尾，不回報；2 秒與 5 秒整不超過 5 秒。
        self.assertEqual(find_transcript_gaps(segments), [])

    def test_overlapping_segments_use_furthest_coverage(self) -> None:
        from pipeline import find_transcript_gaps

        segments = [
            {"start": 0.0, "end": 100.0, "text": "長段", "chunk": 0},
            {"start": 10.0, "end": 20.0, "text": "巢狀段", "chunk": 0},
            {"start": 200.0, "end": 210.0, "text": "後段", "chunk": 1},
        ]
        # 覆蓋已到 100：斷層是 100–200，不是 20–200。
        self.assertEqual(find_transcript_gaps(segments), [{"start": 100.0, "end": 200.0}])


class TranscribeBackfillTest(unittest.TestCase):
    def _chunks(self, root: Path) -> list[dict]:
        import pipeline

        chunks = []
        for index, (start, end) in enumerate([(0.0, 120.0), (120.0, 166.0)]):
            audio = root / f"audio_{index:04d}.mp3"
            audio.write_bytes(f"fixture-{index}".encode())
            chunks.append(
                {
                    "index": index,
                    "path": str(audio),
                    "source_start": start,
                    "source_end": end,
                    "size_bytes": audio.stat().st_size,
                    "sha256": pipeline.file_sha256(audio),
                }
            )
        return chunks

    def test_backfills_issue55_gap(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)
            calls: list[str] = []
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                calls.append(Path(str(audio_path)).name)
                if "gap" in Path(str(audio_path)).name:
                    return {
                        "language": "zh",
                        "segments": [
                            # 15 秒上下文重轉：斷層外的段落必須去重；
                            # 斷層內的段落必須合併。
                            {"start": 0.0, "end": 8.0, "text": "而且还能看到逐年的趋势"},
                            {"start": 8.0, "end": 17.0, "text": "而且还能看到逐年的趋势"},
                            {"start": 17.0, "end": 32.0, "text": "所有上传的资料都能在这里找到"},
                            {"start": 32.0, "end": 44.6, "text": "当真到陪他们去看病的时候"},
                            {"start": 44.6, "end": 54.6, "text": "最后能变成生活里用得上的东西"},
                        ],
                    }
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [
                            {"start": 100.0, "end": 117.9,
                             "text": "会自动归类到费用这个页面里面去"},
                            {"start": 117.9, "end": 120.0, "text": "而且还能看到逐年的趋势"},
                        ],
                    }
                return {
                    "language": "zh",
                    "segments": [
                        {"start": 29.6, "end": 35.0, "text": "就像这个工作台"},
                        {"start": 35.0, "end": 46.0, "text": "最后能变成生活里用得上的东西"},
                    ],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                # 斷層 [120, 149.6] 前後各取 15 秒上下文。
                self.assertAlmostEqual(start, 105.0)
                self.assertAlmostEqual(end, 164.6)
                dest.write_bytes(b"gap-audio")
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                transcript, _ = pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2,
                    root / "cache", source_path=root / "source.mp4",
                )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract

            segments = transcript["segments"]
            # 斷層內的字幕內容必須出現（驗收：2:00–2:28）。
            joined = "".join(seg["text"] for seg in segments)
            self.assertIn("所有上传的资料都能在这里找到", joined)
            self.assertIn("当真到陪他们去看病的时候", joined)
            # 斷層外上下文不得重複寫入逐字稿。
            self.assertEqual(joined.count("而且还能看到逐年的趋势"), 1)
            # 回填後 0–166 不得再有超過 5 秒的內部空白。
            ordered = sorted(segments, key=lambda seg: seg["start"])
            worst = max(
                (nxt["start"] - cur["end"] for cur, nxt in zip(ordered, ordered[1:])),
                default=0.0,
            )
            self.assertLessEqual(worst, 5.0)
            # 回填段落不得超出斷層 ±1 秒：邊界重複最多一個詞，不能整句重貼。
            for seg in segments:
                if str(seg.get("chunk", "")).startswith("gap-"):
                    self.assertGreaterEqual(seg["start"], 120.0 - 1.0)
                    self.assertLessEqual(seg["end"], 149.6 + 1.0)
            # 回填Receipt：機器可驗的驗收狀態。
            self.assertEqual(transcript["gap_check"]["remaining"], [])

    def _check_backfill_filter(self, segment: dict, accepted: bool) -> None:
        import tempfile
        from unittest.mock import patch

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                name = Path(audio_path).name
                if "gap" in name:
                    return {"segments": [{"start": 15.0, "end": 44.6, **segment}]}
                if "0000" in name:
                    return {"segments": [{"start": 0.0, "end": 120.0, "text": "第一段內容"}]}
                return {"segments": [{"start": 29.6, "end": 46.0, "text": "第二段內容"}]}

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                dest.write_bytes(b"gap-audio")
                return dest

            with patch.object(pipeline, "groq_transcribe", side_effect=fake_transcribe) as transcribe, \
                    patch.object(pipeline, "_extract_gap_audio", side_effect=fake_extract):
                # 第二次從舊格式快取讀取，仍須在合併時過濾。
                for _ in range(2):
                    transcript, _ = pipeline.transcribe_all(
                        chunks, "key", "whisper-large-v3", "auto", 2,
                        root / "cache", source_path=root / "source.mp4",
                    )
                    backfills = [s for s in transcript["segments"] if s["chunk"] == "gap-0"]
                    self.assertEqual(len(backfills), int(accepted))
                    self.assertEqual(
                        transcript["gap_check"]["remaining"],
                        [] if accepted else [{"start": 120.0, "end": 149.6}],
                    )
                    self.assertEqual(transcript["backfill_errors"], [])
                self.assertEqual(transcribe.call_count, 3)

    def test_backfill_rejects_english_hallucinations(self) -> None:
        for text in ("Thank you.", "Thanks for watching", "you", "Please subscribe"):
            with self.subTest(text=text):
                self._check_backfill_filter({"text": text, "no_speech_prob": 0.3}, False)

    def test_backfill_rejects_chinese_hallucinations(self) -> None:
        for text in ("字幕由Amara.org社区提供", "请不吝点赞订阅转发打赏支持明镜与点点栏目", "谢谢观看"):
            with self.subTest(text=text):
                self._check_backfill_filter({"text": text}, False)

    def test_backfill_rejects_low_logprob(self) -> None:
        self._check_backfill_filter({"text": "這是正常句子", "avg_logprob": -1.5}, False)

    def test_backfill_rejects_high_compression_ratio(self) -> None:
        self._check_backfill_filter({"text": "這是正常句子", "compression_ratio": 3.0}, False)

    def test_backfill_rejects_nearby_duplicates(self) -> None:
        for start, end, text in (
            (14.0, 44.6, "第一段，內容！"),
            (16.0, 44.6, "第一段內"),
            (15.0, 45.6, "第二段內容還有後續"),
        ):
            with self.subTest(start=start, end=end, text=text):
                self._check_backfill_filter({"start": start, "end": end, "text": text}, False)

    def test_backfill_accepts_normal_speech(self) -> None:
        self._check_backfill_filter(
            {"text": "這是正常句子", "avg_logprob": -0.3, "compression_ratio": 1.5}, True,
        )

    def test_backfill_accepts_short_contained_text(self) -> None:
        from pipeline import accept_backfill_segment

        for text, previous in (("是", "是這樣沒錯"), ("是這樣沒錯", "是"),
                               ("ok", "looks ok"), ("第一段", "第一段內容")):
            with self.subTest(text=text, previous=previous):
                self.assertTrue(accept_backfill_segment(
                    {"start": 10.0, "end": 11.0, "text": text},
                    [{"start": 9.0, "end": 10.0, "text": previous}],
                ))

    def test_backfill_rejects_long_contained_text(self) -> None:
        from pipeline import accept_backfill_segment

        for text, previous in (("是這樣沒錯", "對，是這樣沒錯啊"),
                               ("第一段內", "第一段內容"), ("第一段內容", "第一段內")):
            with self.subTest(text=text, previous=previous):
                self.assertFalse(accept_backfill_segment(
                    {"start": 10.0, "end": 11.0, "text": text},
                    [{"start": 9.0, "end": 10.0, "text": previous}],
                ))

    def test_backfill_rejects_identical_short_text(self) -> None:
        from pipeline import accept_backfill_segment

        self.assertFalse(accept_backfill_segment(
            {"start": 10.0, "end": 11.0, "text": "好"},
            [{"start": 9.0, "end": 10.0, "text": "好"}],
        ))

    def test_backfill_accepts_missing_confidence(self) -> None:
        self._check_backfill_filter({"text": "這是正常句子"}, True)

    def test_backfill_accepts_nonfinite_confidence(self) -> None:
        for value in (float("nan"), float("-inf"), float("inf"), None, "bad", True):
            with self.subTest(value=value):
                self._check_backfill_filter(
                    {"text": "這是正常句子", "avg_logprob": value, "compression_ratio": value}, True,
                )

    def test_backfill_accepts_threshold_boundaries(self) -> None:
        self._check_backfill_filter(
            {"text": "這是正常句子", "avg_logprob": -1.0, "compression_ratio": 2.4}, True,
        )

    def test_backfill_accepts_distinct_or_distant_text(self) -> None:
        for text in ("第一段內容延伸出不同語句", "這句提到 Thank you 但有正常內容"):
            with self.subTest(text=text):
                # 起點距前段 2 秒：文字包含也不能當作重複。
                self._check_backfill_filter({"start": 17.0, "text": text}, True)

    def test_backfill_result_is_cached(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.mp4").write_bytes(b"video")
            chunks = self._chunks(root)
            calls = 0
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                nonlocal calls
                calls += 1
                if "gap" in Path(str(audio_path)).name:
                    return {
                        "language": "zh",
                        "segments": [{"start": 15.0, "end": 44.6, "text": "補上斷層"}],
                    }
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [{"start": 0.0, "end": 120.0, "text": "第一段"}],
                    }
                return {
                    "language": "zh",
                    "segments": [{"start": 29.6, "end": 46.0, "text": "第二段"}],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                dest.write_bytes(b"gap-audio")
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2,
                    root / "cache", source_path=root / "source.mp4",
                )
                first_calls = calls
                pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2,
                    root / "cache", source_path=root / "source.mp4",
                )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract
            # 第二次整批命中快取：2 切段 + 1 回填都不再呼叫 API。
            self.assertEqual(first_calls, 3)
            self.assertEqual(calls, 3)

    def test_without_source_path_gap_is_left_alone(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)
            original_transcribe = pipeline.groq_transcribe

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [{"start": 0.0, "end": 120.0, "text": "第一段"}],
                    }
                return {
                    "language": "zh",
                    "segments": [{"start": 29.6, "end": 46.0, "text": "第二段"}],
                }

            pipeline.groq_transcribe = fake_transcribe
            try:
                transcript, _ = pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2, root / "cache"
                )
            finally:
                pipeline.groq_transcribe = original_transcribe
            from pipeline import find_transcript_gaps

            self.assertEqual(len(find_transcript_gaps(transcript["segments"])), 1)

    def test_largest_gap_backfilled_first(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.mp4").write_bytes(b"video")
            chunks = []
            for index, (start, end) in enumerate([(0.0, 160.0), (160.0, 230.0)]):
                audio = root / f"audio_{index:04d}.mp3"
                audio.write_bytes(f"fixture-{index}".encode())
                chunks.append(
                    {
                        "index": index,
                        "path": str(audio),
                        "source_start": start,
                        "source_end": end,
                        "size_bytes": audio.stat().st_size,
                        "sha256": pipeline.file_sha256(audio),
                    }
                )
            windows: list[tuple[float, float]] = []
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                name = Path(str(audio_path)).name
                if "gap" in name:
                    payload = Path(str(audio_path)).read_bytes().decode()
                    window_start, window_end = (float(v) for v in payload.split(","))
                    width = window_end - window_start
                    return {
                        "language": "zh",
                        "segments": [
                            {"start": 14.5, "end": width - 14.5, "text": "補上斷層"},
                        ],
                    }
                index = 0 if "0000" in name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [
                            {"start": s, "end": s + 10.0, "text": f"甲{s:g}"}
                            for s in (0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 120.0, 140.0)
                        ],
                    }
                return {
                    "language": "zh",
                    "segments": [
                        {"start": 0.0, "end": 10.0, "text": "乙160"},
                        {"start": 50.0, "end": 60.0, "text": "乙210"},
                    ],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                windows.append((start, end))
                dest.write_bytes(f"{start},{end}".encode())
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                transcript, _ = pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2,
                    root / "cache", source_path=root / "source.mp4",
                )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract

            # 9 個斷層（8 個 10 秒＋1 個 40 秒），上限 8 次：40 秒的最大斷層必須補到。
            self.assertEqual(len(windows), 8)
            self.assertIn((155.0, 220.0), windows)
            # 同為 10 秒時先補後面的：最早的 10–20 留到下次（有快取，重跑只付那一段）。
            from pipeline import find_transcript_gaps

            self.assertEqual(
                transcript["gap_check"]["remaining"], [{"start": 10.0, "end": 20.0}]
            )
            self.assertEqual(find_transcript_gaps(transcript["segments"]), [{"start": 10.0, "end": 20.0}])

    def test_malformed_backfill_segment_skipped(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                if "gap" in Path(str(audio_path)).name:
                    return {
                        "language": "zh",
                        "segments": [
                            "oops",
                            {"start": "oops", "end": None, "text": "壞掉的時間戳"},
                            {"start": 15.0, "end": 40.0, "text": "斷層內的有效段落"},
                        ],
                    }
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [{"start": 0.0, "end": 120.0, "text": "第一段"}],
                    }
                return {
                    "language": "zh",
                    "segments": [{"start": 29.6, "end": 46.0, "text": "第二段"}],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                dest.write_bytes(b"gap-audio")
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                transcript, _ = pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2,
                    root / "cache", source_path=root / "source.mp4",
                )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract

            joined = "".join(seg["text"] for seg in transcript["segments"])
            self.assertIn("斷層內的有效段落", joined)
            self.assertNotIn("壞掉的時間戳", joined)

    def test_oversize_backfill_recorded_not_raised(self) -> None:
        import os
        import tempfile
        from unittest import mock

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [{"start": 0.0, "end": 120.0, "text": "第一段"}],
                    }
                return {
                    "language": "zh",
                    "segments": [{"start": 29.6, "end": 46.0, "text": "第二段"}],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                dest.write_bytes(b"x" * 200)
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                # 切段檔只有 9 位元組可過；200 位元組的補轉檔超標：
                # 不丟整份逐字稿，只記一筆 backfill_errors。
                with mock.patch.dict(os.environ, {"GROQ_MAX_UPLOAD_MB": "0.0001"}):
                    transcript, _ = pipeline.transcribe_all(
                        chunks, "key", "whisper-large-v3", "auto", 2,
                        root / "cache", source_path=root / "source.mp4",
                    )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract
            self.assertEqual(len(transcript["segments"]), 2)
            self.assertEqual(len(transcript["backfill_errors"]), 1)
            self.assertIn("上傳限制", transcript["backfill_errors"][0]["error"])
            self.assertEqual(
                transcript["gap_check"]["remaining"], [{"start": 120.0, "end": 149.6}]
            )

    def test_empty_transcript_reports_gap_check(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            transcript, _ = pipeline.transcribe_all(
                [], "key", "whisper-large-v3", "auto", 1, Path(temporary)
            )
            self.assertEqual(
                transcript["gap_check"], {"max_gap": 5.0, "remaining": []}
            )
            self.assertEqual(transcript["backfill_errors"], [])

    def test_backfill_failure_keeps_initial_transcript(self) -> None:
        import tempfile

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                if "gap" in Path(str(audio_path)).name:
                    raise RuntimeError("Groq 503")
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [{"start": 0.0, "end": 120.0, "text": "第一段"}],
                    }
                return {
                    "language": "zh",
                    "segments": [{"start": 29.6, "end": 46.0, "text": "第二段"}],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                dest.write_bytes(b"gap-audio")
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                transcript, _ = pipeline.transcribe_all(
                    chunks, "key", "whisper-large-v3", "auto", 2,
                    root / "cache", source_path=root / "source.mp4",
                )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract

            # 初版切段結果保留，失敗如實記載，斷層仍回報為未補。
            self.assertEqual(len(transcript["segments"]), 2)
            self.assertEqual(len(transcript["backfill_errors"]), 1)
            self.assertIn("Groq 503", transcript["backfill_errors"][0]["error"])
            self.assertEqual(
                transcript["gap_check"]["remaining"], [{"start": 120.0, "end": 149.6}]
            )

    def test_budget_block_still_aborts_backfill(self) -> None:
        import tempfile

        from cost_tracker import BudgetBlockedError

        import pipeline

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = self._chunks(root)
            original_transcribe = pipeline.groq_transcribe
            original_extract = pipeline._extract_gap_audio

            def fake_transcribe(audio_path: Path, *args, **kwargs) -> dict:
                if "gap" in Path(str(audio_path)).name:
                    raise BudgetBlockedError("budget exceeded")
                index = 0 if "0000" in Path(str(audio_path)).name else 1
                if index == 0:
                    return {
                        "language": "zh",
                        "segments": [{"start": 0.0, "end": 120.0, "text": "第一段"}],
                    }
                return {
                    "language": "zh",
                    "segments": [{"start": 29.6, "end": 46.0, "text": "第二段"}],
                }

            def fake_extract(source: Path, start: float, end: float, dest: Path) -> Path:
                dest.write_bytes(b"gap-audio")
                return dest

            pipeline.groq_transcribe = fake_transcribe
            pipeline._extract_gap_audio = fake_extract
            try:
                with self.assertRaises(BudgetBlockedError):
                    pipeline.transcribe_all(
                        chunks, "key", "whisper-large-v3", "auto", 2,
                        root / "cache", source_path=root / "source.mp4",
                    )
            finally:
                pipeline.groq_transcribe = original_transcribe
                pipeline._extract_gap_audio = original_extract


if __name__ == "__main__":
    unittest.main()
