"""Unreliable visual time stays searchable without source seek links."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


URL = "https://www.youtube.com/watch?v=fixture"


class VisualTimeReliabilitySearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job = self.root / "jobs" / "abc123456789"
        self.job.mkdir(parents=True)
        self.manifest = {"status": "completed", "source": {"url": URL, "title": "測試影片"},
                         "video": {"duration_seconds": 100}}
        self.timeline = [
            self.visual(5, "終端機錯誤甲", analysis_method="minimax_video", timestamp_reliable=False,
                        timestamp_issue="時間碼疑似被壓縮", timestamp_original="原始時間雜訊"),
            self.visual(6, "終端機錯誤乙", analysis_method="minimax_video"),
            self.visual(90, "可靠畫面", analysis_method="minimax_video", timestamp_reliable=True),
            self.visual(95, "抽幀畫面", analysis_method="minimax_video_fallback_frames"),
        ]
        self.summary = {"short_summary": "摘要", "chapters": [
            {"start": 5, "end": 10, "title": "不可靠章節", "summary": "保留內容"},
            {"start": 90, "end": 100, "title": "可靠章節"},
        ]}
        pipeline.write_json(self.job / "manifest.json", self.manifest)
        self.write_artifacts()

    @staticmethod
    def visual(stamp, text="測試畫面", **data):
        return {"type": "visual", "timestamp": stamp, "data": {"visual_summary": text, **data}}

    def write_artifacts(self):
        pipeline.write_json(self.job / "timeline.json", self.timeline)
        pipeline.write_json(self.job / "summary.json", self.summary)

    def rows(self):
        with contextlib.closing(pipeline.index_connection(self.root)) as connection:
            return connection.execute("SELECT timestamp, kind, text FROM events ORDER BY rowid").fetchall()

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             mock.patch("sys.argv", ["pipeline.py", "--root", str(self.root), *args]), \
             mock.patch.object(pipeline, "load_dotenv"), \
             mock.patch.object(pipeline, "minimax_subscription_key", return_value="fake-key"), \
             mock.patch.object(pipeline, "minimax_model", return_value="fake-model"):
            self.assertEqual(pipeline.main(), 0)
        return output.getvalue()

    def test_visual_time_reliable_flags_legacy_and_bad_data(self):
        for data, expected in [({"timestamp_reliable": False}, False),
                               ({"timestamp_reliable": True, "analysis_method": "minimax_video"}, True),
                               ({"analysis_method": "minimax_video"}, False),
                               ({"analysis_method": "minimax_video_compressed"}, False),
                               ({"analysis_method": "minimax_video_fallback_frames"}, True),
                               ({"analysis_method": "frames"}, True),
                               ({"analysis_method": "local"}, True),
                               ({}, True), (None, True), ([], True),
                               ({"timestamp_reliable": "false", "analysis_method": "minimax_video"}, False),
                               ({"analysis_method": []}, True)]:
            with self.subTest(data=data):
                self.assertIs(pipeline.visual_time_reliable(data), expected)

    def test_unreliable_seconds_reliable_collision_and_bad_data(self):
        timeline = [self.visual(2.1, timestamp_reliable=False),
                    {"type": "speech", "timestamp": 2.9, "text": "可靠語音"},
                    self.visual(3.1, timestamp_reliable=False), self.visual(3.9),
                    self.visual("5.2", analysis_method="minimax_video"),
                    None, [], {}, {"type": "visual", "data": None}]
        timeline.extend(self.visual(stamp, timestamp_reliable=False)
                        for stamp in (None, "invalid", float("nan"), float("inf"), -float("inf"), [], {}))
        self.assertEqual(pipeline.unreliable_visual_seconds(timeline), {5})
        for value in (None, {}, "invalid", 5):
            self.assertEqual(pipeline.unreliable_visual_seconds(value), set())

    def render(self, timeline, summary):
        path = pipeline.render_knowledge_markdown(
            self.job, self.manifest["source"], self.job / "clip.mp4", {"segments": []}, [], timeline, summary)
        return path.read_text(encoding="utf-8").splitlines()

    def test_markdown_chapters_fact_checks_and_same_second_speech(self):
        timeline = [self.visual(2.0, analysis_method="minimax_video"),
                    {"type": "speech", "timestamp": 90, "text": "可靠語音"}]
        summary = {"chapters": [{"start": 2.0, "title": "第一章", "summary": "內容保留"},
                                {"start": 90, "title": "第二章"}],
                   "fact_check": [{"timestamp": 2.0, "claim": "待查主張", "reason": "理由保留"}]}
        lines = self.render(timeline, summary)
        for text in ("第一章", "待查主張", "畫面："):
            line = next(line for line in lines if text in line)
            self.assertIn("時間碼不可靠", line)
            self.assertNotIn("](https://", line)
        self.assertTrue(any("內容保留" in line for line in lines))
        self.assertTrue(any("理由保留" in line for line in lines))
        self.assertIn("&t=90s", next(line for line in lines if "第二章" in line))
        timeline.append({"type": "speech", "timestamp": 2.9, "text": "可靠語音"})
        lines = self.render(timeline, summary)
        for text in ("第一章", "待查主張"):
            self.assertIn("&t=2s", next(line for line in lines if text in line))

    def test_markdown_tolerates_nonnumeric_summary_times(self):
        lines = self.render([], {"chapters": [{"start": "未知", "end": "稍後", "title": "章節"}],
                                 "fact_check": [{"timestamp": "未知", "claim": "待查主張"}]})
        self.assertIn(URL, next(line for line in lines if "待查主張" in line))
        self.assertTrue(any("稍後" in line for line in lines))

    def test_index_null_times_preserves_kind_count_and_reliable_times(self):
        self.assertEqual(pipeline.build_index(self.root)[1:], (1, 7))
        rows = self.rows()
        self.assertEqual([(stamp, kind) for stamp, kind, text in rows],
                         [(0, "title"), (None, "visual"), (None, "visual"),
                          (90, "visual"), (0, "summary"),
                          (None, "chapter"), (90, "chapter")])
        self.assertFalse(any("抽幀畫面" in text for stamp, kind, text in rows))
        self.timeline.append({"type": "speech", "timestamp": 5.9, "text": "可靠語音"})
        self.write_artifacts()
        self.assertEqual(pipeline.build_index(self.root)[1:], (1, 8))
        self.assertEqual(next(stamp for stamp, kind, text in self.rows()
                              if kind == "chapter" and "不可靠章節" in text), 5)

    def test_index_excludes_timestamp_metadata_from_search_and_vector_text(self):
        pipeline.build_index(self.root)
        for query in ("時間碼疑似被壓縮", "原始時間雜訊"):
            self.assertEqual(pipeline.search_index(self.root, query, 10), [])
        visual_text = [text for stamp, kind, text in self.rows() if kind == "visual"]
        self.assertTrue(any("終端機錯誤甲" in text for text in visual_text))
        self.assertFalse(any("時間碼疑似被壓縮" in text or "原始時間雜訊" in text for text in visual_text))

    def test_cli_search_unreliable_has_no_link_reliable_keeps_link(self):
        pipeline.build_index(self.root)
        output = self.cli("--search", "終端機")
        self.assertEqual(output.count("時間碼不可靠"), 2)
        self.assertNotIn("t=", output)
        self.assertNotIn(URL, output)
        self.assertNotIn("&t=95s", self.cli("--search", "抽幀畫面"))

    def test_semantic_search_uses_null_time_and_shared_cli_output(self):
        pipeline.build_index(self.root)
        with contextlib.closing(pipeline.index_connection(self.root)) as connection:
            for stamp, kind, text in self.rows():
                if kind == "visual" and stamp is None:
                    connection.execute("INSERT INTO vectors VALUES (?, ?, ?, ?)",
                                       (pipeline.text_hash(text), "fake-model", 2, pipeline.pack_vector([1.0, 0.0])))
            connection.commit()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0]]):
            hits = pipeline.semantic_search(self.root, "問題", "", "fake-model", 10, "local")
            self.assertEqual(len(hits), 2)
            self.assertTrue(all(hit["timestamp"] is None for hit in hits))
            output = self.cli("--search", "問題", "--semantic", "--embed-backend", "local",
                              "--embed-model", "fake-model")
        self.assertEqual(output.count("時間碼不可靠"), 2)
        self.assertNotIn("t=", output)

    def test_ask_prompt_deduplication_and_citations_without_seek_links(self):
        pipeline.build_index(self.root)
        with mock.patch.object(pipeline, "question_keywords", return_value=["終端機", "錯誤"]), \
             mock.patch.object(pipeline, "minimax_chat", return_value="測試答案 [1] [2]") as chat:
            result = pipeline.answer_question(self.root, "問題", "fake-key", "fake-model", 10)
            self.assertEqual(len(result["citations"]), 2)
            self.assertTrue(all(hit["timestamp"] is None for hit in result["citations"]))
            output = self.cli("--ask", "問題")
        prompt = chat.call_args.args[0][0]["content"]
        self.assertNotIn("00:00:05", prompt)
        self.assertNotIn("00:00:00", prompt)
        self.assertEqual(prompt.count("時間碼不可靠"), 2)
        self.assertIn("終端機錯誤甲", prompt)
        self.assertIn("終端機錯誤乙", prompt)
        self.assertEqual(output.count("時間碼不可靠"), 2)
        self.assertNotIn("t=", output)
        self.assertNotIn(URL, output)

    def test_signature_v1_rebuilds_null_times_without_deleting_vectors(self):
        pipeline.build_index(self.root)
        source = self.manifest["source"]
        old_signature = pipeline.text_hash(json.dumps({
            "version": 1, "source": {"title": source["title"], "url": source["url"], "video": None},
            "status": "completed", "duration": 100, "categories": [],
            "timeline": pipeline.file_fingerprint(self.job / "timeline.json"),
            "summary": pipeline.file_fingerprint(self.job / "summary.json"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        with contextlib.closing(pipeline.index_connection(self.root)) as connection:
            connection.execute("UPDATE index_state SET signature = ?", (old_signature,))
            connection.execute("UPDATE events SET timestamp = 5.0 WHERE kind = 'visual'")
            connection.execute("INSERT INTO vectors VALUES ('unchanged', 'fake-model', 1, x'00')")
            connection.commit()
        self.assertEqual(pipeline.build_index(self.root)[1:], (1, 7))
        self.assertEqual([stamp for stamp, kind, text in self.rows() if kind == "visual"], [None, None, 90])
        with contextlib.closing(pipeline.index_connection(self.root)) as connection:
            self.assertNotEqual(connection.execute("SELECT signature FROM index_state").fetchone()[0], old_signature)
            self.assertEqual(connection.execute("SELECT count(*) FROM vectors").fetchone()[0], 1)
            changes = connection.total_changes
            self.assertEqual(pipeline.index_job(connection, self.job), 7)
            self.assertEqual(connection.total_changes, changes)

    def test_pre_upgrade_library_drops_visual_rows_before_any_search(self):
        pipeline.build_index(self.root)
        with contextlib.closing(pipeline.index_connection(self.root)) as connection:
            connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                               (self.job.name, 95, "visual", "舊抽幀畫面殘留"))
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        # No --index run: opening the library for search must already purge it.
        self.assertEqual(pipeline.search_index(self.root, "舊抽幀畫面殘留", 10), [])
        with contextlib.closing(pipeline.index_connection(self.root)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events WHERE kind = 'visual'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM index_state").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], pipeline.LIBRARY_SCHEMA_VERSION)
        self.assertEqual(pipeline.build_index(self.root)[1:], (1, 7))

    def test_index_bad_timeline_leaves_chapters_untainted(self):
        (self.job / "timeline.json").write_text("invalid JSON", encoding="utf-8")
        self.assertEqual(pipeline.build_index(self.root)[1:], (1, 4))
        self.assertEqual([stamp for stamp, kind, text in self.rows() if kind == "chapter"], [5, 90])

    def test_missing_chapter_start_does_not_match_unreliable_zero(self):
        self.timeline = [self.visual(0, timestamp_reliable=False)]
        self.summary["chapters"] = [{"title": "未給時間"}, {"start": 0, "title": "零秒章節"}]
        self.write_artifacts()
        pipeline.build_index(self.root)
        self.assertEqual([stamp for stamp, kind, text in self.rows() if kind == "chapter"], [0, None])

    def test_show_video_tainted_chapter_has_no_seek_link(self):
        output = self.cli("--show", "測試影片")
        self.assertIn("時間碼不可靠", next(line for line in output.splitlines() if "不可靠章節" in line))
        self.assertNotIn("&t=5s", output)
        self.assertIn("&t=90s", output)


if __name__ == "__main__":
    unittest.main()
