"""Actual-index, offline regressions for issue #5's citation/cache identity."""

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline
from embedding_contract_fixture import fictional_embedding_contract, seed_contract
from test_retrieval import seed_video


class EvidenceIdentityCacheTests(unittest.TestCase):
    def setUp(self):
        contract = mock.patch.object(pipeline, "embedding_contract", side_effect=fictional_embedding_contract)
        contract.start()
        self.addCleanup(contract.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()

    def seed(self, root=None, **changes):
        root = root or self.root
        event = {"type": "speech", "timestamp": 9, "end": 12,
                 "segment_id": "original-7", "text": "quoted original sentence"}
        event.update(changes)
        return seed_video(root, "same-job", url="https://example.test/one", timeline=[event])

    def hit(self, root=None):
        root = root or self.root
        pipeline.build_index(root)
        return next(h for h in pipeline.search_index(root, "quoted original", 10) if h["kind"] == "speech")

    def test_separate_libraries_namespace_identical_jobs_and_sources(self):
        other = Path(self.temp.name) / "other"
        self.seed(); self.seed(other)
        first, second = self.hit(), self.hit(other)
        self.assertNotEqual(first["occurrence_id"], second["occurrence_id"])
        self.assertNotEqual(first["library_id"], second["library_id"])
        self.assertEqual(first["analysis_revision"], second["analysis_revision"])

    def test_source_and_explicit_analysis_revision_change_citation_identity(self):
        job = self.seed()
        previous = self.hit()
        manifest_path = job / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for field, value in (("url", "https://example.test/two"), ("analysis_revision", "analysis-2")):
            if field == "url": manifest["source"][field] = value
            else: manifest[field] = value
            pipeline.write_json(manifest_path, manifest)
            current = self.hit()
            self.assertNotEqual(previous["occurrence_id"], current["occurrence_id"])
            self.assertNotEqual(previous["analysis_revision"], current["analysis_revision"])
            previous = current

    def test_range_null_kind_and_original_segment_changes_are_bound(self):
        self.seed()
        previous = self.hit()
        for changes in ({"end": 20}, {"end": None}, {"timestamp": None, "end": None},
                        {"timestamp": 9, "type": "visual", "text": "", "data": "quoted original sentence"},
                        {"segment_id": "original-8"}):
            self.seed(**changes)
            pipeline.build_index(self.root)
            current = pipeline.search_index(self.root, "quoted original", 10)[0]
            self.assertNotEqual(previous["occurrence_id"], current["occurrence_id"])
            self.assertEqual(len(current["occurrence_id"]), 68)
            self.assertEqual(current["original_segment_ids"], [changes.get("segment_id", "original-7")])
            previous = current

    def test_identity_survives_move_reindex_and_derived_database_recreation(self):
        self.seed(); before = self.hit()
        (self.root / "library.db").unlink()
        self.assertEqual(before, self.hit())
        moved = Path(self.temp.name) / "moved"
        self.root.rename(moved)
        self.assertEqual(before, self.hit(moved))
        self.assertEqual(before["original_segment_ids"], ["original-7"])

    def test_unknown_times_remain_null_in_search_context_and_citation(self):
        self.seed(timestamp=None, end=None)
        hit = self.hit()
        self.assertIsNone(hit["start"]); self.assertIsNone(hit["timestamp"])
        self.assertEqual(hit["timestamp_kind"], "unknown")
        self.assertEqual(pipeline.format_hit_timestamp(hit), "—")
        self.assertEqual(pipeline.format_hit_timestamp({"timestamp": 0}), "00:00:00")
        block = pipeline.pack_context([hit])["blocks"][0]
        citation = pipeline._evidence_citation(block)
        self.assertIsNone(citation["timestamp"])
        self.assertEqual(citation["library_id"], hit["library_id"])
        self.assertEqual(citation["analysis_revision"], hit["analysis_revision"])
        self.assertEqual(citation["original_segment_ids"], ["original-7"])

    def test_full_text_digest_and_identical_text_vector_reuse(self):
        self.assertEqual(pipeline.text_hash("alpha"), hashlib.sha256(b"alpha").hexdigest())
        seed_video(self.root, "a", timeline=[{"type": "speech", "text": "alpha"}])
        seed_video(self.root, "b", timeline=[{"type": "speech", "text": "alpha"}])
        pipeline.build_index(self.root)
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]) as embed, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local"), (1, 1))
            self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local"), (0, 1))
        embed.assert_called_once()
        self.assertEqual(len(pipeline.search_index(self.root, "alpha", 10)), 2)

    def test_injected_hash_collision_rejected_before_embedding_or_cache_write(self):
        seed_video(self.root, "a", timeline=[{"type": "speech", "text": text} for text in ("alpha", "beta")])
        pipeline.build_index(self.root)
        with mock.patch.object(pipeline, "text_hash", return_value="0" * 16), mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]) as embed:
            with self.assertRaisesRegex(RuntimeError, "hash|雜湊|collision"):
                pipeline.build_embeddings(self.root, "", "offline", "local")
        embed.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vectors").fetchone()[0], 0)

    def test_collision_with_existing_cached_text_is_also_rejected(self):
        seed_video(self.root, "a", timeline=[{"type": "speech", "text": "alpha"}])
        pipeline.build_index(self.root)
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
            pipeline.build_embeddings(self.root, "", "offline", "local")
        seed_video(self.root, "b", timeline=[{"type": "speech", "text": "beta"}])
        pipeline.build_index(self.root)
        with mock.patch.object(pipeline, "text_hash", return_value=pipeline.text_hash("alpha")), mock.patch.object(pipeline, "embed_texts") as embed:
            with self.assertRaisesRegex(RuntimeError, "hash|雜湊|collision"):
                pipeline.build_embeddings(self.root, "", "offline", "local")
        embed.assert_not_called()

    def test_legacy_vector_hash_migrates_locally_without_new_provider_work(self):
        self.seed(); self.hit()
        key = pipeline.vector_namespace("local", "offline")
        text = "quoted original sentence"
        legacy = hashlib.sha256(text.encode()).hexdigest()[:16]
        vector = pipeline.pack_vector([1., 0.])
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("INSERT INTO vectors VALUES (?, ?, 2, ?)", (legacy, key, vector)); db.commit()
            self.assertEqual(seed_contract(db, "local", "offline"), key); db.commit()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            pipeline.build_index(self.root)
            self.assertEqual(pipeline.plan_embeddings(self.root, "local", "offline")["reusable"], 1)
            self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local")[0], 0)
        embed.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertEqual(db.execute("SELECT vector FROM vectors WHERE text_hash=? AND model=?", (hashlib.sha256(text.encode()).hexdigest(), key)).fetchone()[0], vector)

    def test_invalid_library_identity_fails_closed_without_rotation(self):
        self.seed(); self.hit()
        identity = self.root / "library-id.json"
        identity.write_text('{"library_id":""}')
        with self.assertRaisesRegex(RuntimeError, "library|身分"):
            pipeline.build_index(self.root)
        self.assertEqual(identity.read_text(), '{"library_id":""}')

    def test_ambiguous_legacy_hash_is_quarantined_and_original_vectors_preserved(self):
        texts = ["alpha", "beta"]
        seed_video(self.root, "a", timeline=[{"type": "speech", "text": text} for text in texts])
        pipeline.build_index(self.root)
        key = pipeline.vector_namespace("local", "offline")
        old_vector = pipeline.pack_vector([1., 0.])
        legacy = "0" * 16
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("INSERT INTO vectors VALUES (?, ?, 2, ?)", (legacy, key, old_vector)); db.commit()
            self.assertEqual(seed_contract(db, "local", "offline"), key); db.commit()
        with mock.patch.object(pipeline, "_legacy_text_hash", return_value=legacy), mock.patch.object(pipeline, "embed_texts") as embed:
            pipeline.build_index(self.root)
            self.assertEqual(pipeline.plan_embeddings(self.root, "local", "offline")["reusable"], 0)
            embed.assert_not_called()
            embed.side_effect = lambda inputs, *args: [[1., 0.] if text == "alpha" else [0., 1.] for text in inputs]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local"), (2, 3))
            self.assertEqual(set(embed.call_args.args[0]), set(texts))
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertEqual(db.execute("SELECT vector FROM vectors WHERE text_hash=?", (legacy,)).fetchone()[0], old_vector)
            self.assertEqual(db.execute("SELECT count(*) FROM vector_texts").fetchone()[0], 2)

    def test_cache_original_text_mismatch_blocks_reads_and_generation_without_calls(self):
        self.seed(); self.hit()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
            pipeline.build_embeddings(self.root, "", "offline", "local")
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("UPDATE vector_texts SET text='different original'"); db.commit()
            before = db.execute("SELECT * FROM vectors").fetchall()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            for operation in (lambda: pipeline.plan_embeddings(self.root, "local", "offline"),
                              lambda: pipeline.semantic_search(self.root, "quote", "", "offline", 10, "local"),
                              lambda: pipeline.build_embeddings(self.root, "", "offline", "local")):
                with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "collision"):
                    operation()
        embed.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertEqual(before, db.execute("SELECT * FROM vectors").fetchall())

    def test_old_fts_migration_binds_local_originals_and_preserves_all_vector_bytes(self):
        self.seed()
        text = "quoted original sentence"
        legacy = hashlib.sha256(text.encode()).hexdigest()[:16]
        key = pipeline.vector_namespace("local", "offline")
        vector = pipeline.pack_vector([1., 0.])
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.executescript("CREATE TABLE events(job TEXT,timestamp REAL,kind TEXT,text TEXT);"
                             "CREATE TABLE vectors(text_hash TEXT,model TEXT,dim INTEGER,vector BLOB,PRIMARY KEY(text_hash,model));")
            self.assertEqual(seed_contract(db, "local", "offline"), key)
            db.execute("INSERT INTO events VALUES ('same-job',9,'speech',?)", (text,))
            db.execute("INSERT INTO vectors VALUES (?,?,2,?)", (legacy,key,vector)); db.commit()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            hit = self.hit()
            self.assertEqual(hit["original_segment_ids"], ["original-7"])
            self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local")[0], 0)
        embed.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertEqual(db.execute("SELECT vector FROM vectors ORDER BY text_hash").fetchall(), [(vector,), (vector,)])

    def test_missing_provenance_read_fails_closed_and_explicit_reindex_repairs(self):
        self.seed(); self.hit()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("DROP TABLE occurrence_provenance"); db.commit()
        before = (self.root / "library.db").read_bytes()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            with self.assertRaisesRegex(RuntimeError, "--index"):
                pipeline.semantic_search(self.root, "quote", "", "offline", 10, "local")
        embed.assert_not_called()
        self.assertEqual(before, (self.root / "library.db").read_bytes())
        self.assertTrue(self.hit()["analysis_revision"].startswith("analysis_"))

    def test_chunker_revision_changes_ids_without_reembedding_shared_text(self):
        self.seed(); before = self.hit()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
            pipeline.build_embeddings(self.root, "", "offline", "local")
        with mock.patch.object(pipeline, "CHUNKER_VERSION", "future-test"), mock.patch.object(pipeline, "embed_texts") as embed:
            after = self.hit()
            self.assertNotEqual(before["occurrence_id"], after["occurrence_id"])
            self.assertEqual(before["analysis_revision"], after["analysis_revision"])
            self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local")[0], 0)
        embed.assert_not_called()

    def test_actual_model_and_analysis_recipe_drift_change_revision_without_reembedding(self):
        job = self.seed()
        path = job / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(models={"transcript": "offline-v1"}, config={"frame_interval": 10})
        pipeline.write_json(path, manifest)
        previous = self.hit()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
            pipeline.build_embeddings(self.root, "", "offline", "local")
        for field, value in (("models", {"transcript": "offline-v2"}), ("config", {"frame_interval": 20})):
            manifest[field] = value
            pipeline.write_json(path, manifest)
            with self.subTest(field=field), mock.patch.object(pipeline, "embed_texts") as embed:
                current = self.hit()
                self.assertNotEqual(previous["analysis_revision"], current["analysis_revision"])
                self.assertNotEqual(previous["occurrence_id"], current["occurrence_id"])
                self.assertEqual(pipeline.build_embeddings(self.root, "", "offline", "local")[0], 0)
                embed.assert_not_called()
            previous = current

    def test_runtime_workers_and_notebook_sync_do_not_change_analysis_identity(self):
        job = self.seed(); before = self.hit()
        path = job / "manifest.json"
        manifest = json.loads(path.read_text())
        for field, value in (("workers", 3), ("vision_workers", 2),
                             ("sync_open_notebook", True), ("notebook_id", "offline-notebook")):
            manifest.setdefault("config", {})[field] = value
            pipeline.write_json(path, manifest)
            with self.subTest(field=field):
                after = self.hit()
                self.assertEqual(before["analysis_revision"], after["analysis_revision"])
                self.assertEqual(before["occurrence_id"], after["occurrence_id"])


if __name__ == "__main__":
    unittest.main()
