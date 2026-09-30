import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class EvidenceOccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job_dir = self.root / "jobs" / "job-a"
        self.job_dir.mkdir(parents=True)
        self.manifest = {
            "status": "done",
            "source": {"title": "Example video", "url": "https://youtu.be/example"},
            "video": {"duration_seconds": 120},
        }
        self._write_json(self.job_dir / "manifest.json", self.manifest)

    @staticmethod
    def _write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _write_timeline(self, events):
        self._write_json(self.job_dir / "timeline.json", events)

    def _index(self):
        connection = pipeline.index_connection(self.root)
        try:
            count = pipeline.index_job(connection, self.job_dir)
            connection.commit()
            return count
        finally:
            connection.close()

    def _rowid_for_text(self, text):
        connection = sqlite3.connect(self.root / "library.db")
        try:
            return connection.execute(
                "SELECT rowid FROM events WHERE job = ? AND text = ?", ("job-a", text)
            ).fetchone()[0]
        finally:
            connection.close()

    def test_id_survives_index_rebuild_that_changes_fts_rowid(self):
        stable_text = "needle stable evidence sentence"
        self._write_timeline([{"type": "speech", "timestamp": 120, "end": 124, "text": stable_text}])
        self._index()
        first = next(hit for hit in pipeline.search_index(self.root, "needle stable", 10) if hit["kind"] == "speech")
        first_rowid = self._rowid_for_text(stable_text)

        self._write_timeline(
            [
                {"type": "speech", "timestamp": 10, "text": "unrelated preceding sentence"},
                {"type": "speech", "timestamp": 120, "end": 124, "text": stable_text},
            ]
        )
        self._index()
        second = next(hit for hit in pipeline.search_index(self.root, "needle stable", 10) if hit["kind"] == "speech")
        second_rowid = self._rowid_for_text(stable_text)

        self.assertNotEqual(first_rowid, second_rowid)
        self.assertEqual(first["occurrence_id"], second["occurrence_id"])
        self.assertEqual(second["timestamp"], 120)
        self.assertEqual(second["end"], 124)
        self.assertEqual(second["timestamp_kind"], "range")

    def test_identical_source_segments_get_distinct_deterministic_ids(self):
        repeated = {"type": "speech", "timestamp": 42, "text": "duplicate target marker"}
        self._write_timeline([repeated, repeated])
        self._index()
        first_ids = [hit["occurrence_id"] for hit in pipeline.search_index(self.root, "duplicate target", 10)]

        self._index()
        second_ids = [hit["occurrence_id"] for hit in pipeline.search_index(self.root, "duplicate target", 10)]
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(set(first_ids)), 2)
        self.assertEqual(first_ids, second_ids)

    def test_source_level_and_missing_times_are_null_not_zero(self):
        self.manifest["source"]["title"] = "source level title marker"
        self._write_json(self.job_dir / "manifest.json", self.manifest)
        self._write_timeline(
            [
                {"type": "speech", "text": "unknown time speech marker"},
                {"type": "speech", "timestamp": 0, "text": "real zero time marker"},
            ]
        )
        self._write_json(
            self.job_dir / "summary.json",
            {
                "status": "done",
                "summary": "source level summary marker",
                "chapters": [{"title": "missing chapter time marker", "summary": "chapter detail"}],
            },
        )
        self._index()

        title = pipeline.search_index(self.root, "source level title", 10)[0]
        summary = next(hit for hit in pipeline.search_index(self.root, "source level summary", 10) if hit["kind"] == "summary")
        chapter = next(hit for hit in pipeline.search_index(self.root, "missing chapter time", 10) if hit["kind"] == "chapter")
        unknown = next(hit for hit in pipeline.search_index(self.root, "unknown time speech", 10) if hit["kind"] == "speech")
        zero = next(hit for hit in pipeline.search_index(self.root, "real zero time", 10) if hit["kind"] == "speech")

        for hit in (title, summary):
            self.assertIsNone(hit["timestamp"])
            self.assertEqual(hit["timestamp_kind"], "source_level")
            self.assertEqual(pipeline.format_hit_timestamp(hit), "—")
        for hit in (chapter, unknown):
            self.assertIsNone(hit["timestamp"])
            self.assertEqual(hit["timestamp_kind"], "unknown")
        self.assertEqual(zero["timestamp"], 0)
        self.assertEqual(zero["timestamp_kind"], "point")
        self.assertEqual(pipeline.format_hit_timestamp(zero), "00:00:00")

    def test_semantic_and_ask_results_keep_occurrence_metadata_without_provider_calls(self):
        self.manifest["source"]["title"] = "ask marker title"
        self._write_json(self.job_dir / "manifest.json", self.manifest)
        self._write_timeline([{"type": "speech", "timestamp": 7, "text": "ask marker speech evidence"}])
        self._index()

        connection = pipeline.index_connection(self.root)
        try:
            texts = [row[0] for row in connection.execute("SELECT text FROM events")]
            connection.executemany(
                "INSERT OR REPLACE INTO vectors (text_hash, model, dim, vector) VALUES (?, ?, ?, ?)",
                [(pipeline.text_hash(text), "offline-test", 2, pipeline.pack_vector([1.0, 0.0])) for text in texts],
            )
            connection.commit()
        finally:
            connection.close()

        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0]]) as embed:
            semantic_hits = pipeline.semantic_search(self.root, "ask marker", "", "offline-test", 10, "local")
        embed.assert_called_once()
        speech = next(hit for hit in semantic_hits if hit["kind"] == "speech")
        self.assertTrue(speech["occurrence_id"].startswith("occ_"))
        self.assertEqual(speech["timestamp"], 7)
        self.assertEqual(speech["timestamp_kind"], "point")

        with mock.patch.object(pipeline, "question_keywords", return_value=["ask marker title"]), mock.patch.object(
            pipeline, "minimax_chat", return_value="offline answer"
        ) as completion:
            answer = pipeline.answer_question(self.root, "where is the title?", "", "unused", 5)
        title = next(hit for hit in answer["citations"] if hit["kind"] == "title")
        prompt = completion.call_args.args[0][0]["content"]
        self.assertTrue(title["occurrence_id"].startswith("occ_"))
        self.assertIsNone(title["timestamp"])
        self.assertIn("—", prompt)
        self.assertNotIn("00:00:00", prompt)

    def test_old_unmapped_index_fails_closed_until_indexed_again(self):
        connection = pipeline.index_connection(self.root)
        try:
            connection.execute(
                "INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("job-a", "Example", "", "done", 120, "", "[]"),
            )
            connection.execute(
                "INSERT INTO events (job, timestamp, kind, text) VALUES (?, ?, ?, ?)",
                ("job-a", 0, "summary", "legacy searchable evidence"),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(RuntimeError, "--index"):
            pipeline.search_index(self.root, "legacy searchable", 10)

        self._write_timeline([{"type": "speech", "timestamp": 3, "text": "legacy searchable evidence"}])
        self._index()
        hit = pipeline.search_index(self.root, "legacy searchable", 10)[0]
        self.assertTrue(hit["occurrence_id"].startswith("occ_"))


if __name__ == "__main__":
    unittest.main()
