"""Offline regressions found while reviewing PR #21 against issue #5."""

import contextlib
import io
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

import pipeline
from test_retrieval import fake_embed, fake_embed_batch, seed_library, seed_vectors, seed_video


class ReviewRegressions(unittest.TestCase):
    def test_fts_snippet_uses_text_after_occurrence_column_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            hits = pipeline.search_index(root, "時間軸", 20)
            self.assertTrue(hits)
            for hit in hits:
                self.assertIn("《時間軸》", hit["snippet"])
                self.assertNotIn("occ_", hit["snippet"])

    def test_failed_rebuild_preserves_published_vectors_across_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "many", timeline=[
                {"timestamp": i, "type": "speech", "text": f"fragment {i}"}
                for i in range(pipeline.EMBED_BATCH_SIZE + 1)
            ])
            pipeline.build_index(root)
            key = pipeline.vector_namespace("openrouter", "review-model")
            with contextlib.closing(pipeline.index_connection(root)) as db:
                texts = [row[0] for row in db.execute("SELECT text FROM events")]
                db.executemany("INSERT INTO vectors VALUES (?, ?, 2, ?)", [
                    (pipeline.text_hash(text), key, pipeline.pack_vector([1., 0.]))
                    for text in texts
                ])
                db.execute("INSERT INTO vectors VALUES ('other', 'other-model', 2, ?)",
                           (pipeline.pack_vector([0., 1.]),))
                db.commit()

            def snapshot():
                with contextlib.closing(sqlite3.connect(root / "library.db")) as db:
                    return db.execute("SELECT * FROM vectors ORDER BY model, text_hash").fetchall()

            before = snapshot()
            during = []

            def embed(texts, *args):
                during.append(snapshot())
                if len(during) == 2:
                    raise RuntimeError("second batch failed")
                return [[0., 1.] for _ in texts]

            with mock.patch.object(pipeline, "embed_texts", side_effect=embed):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "second batch failed"):
                        pipeline.rebuild_embeddings(root, "fake", "review-model")
            self.assertEqual(snapshot(), before)
            self.assertEqual(during, [before, before])
            with mock.patch.object(pipeline, "embed_texts", side_effect=lambda texts, *a: [[0., 1.] for _ in texts]):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = pipeline.rebuild_embeddings(root, "fake", "review-model")
            self.assertEqual(result["added"], len(texts))
            self.assertEqual(result["plan"]["pending"], len(texts))
            self.assertEqual(result["plan"]["reusable"], 0)
            self.assertIn(before[-1], snapshot())  # other contract survives

    def test_legacy_remap_does_not_replace_namespaced_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = pipeline.DEFAULT_EMBED_MODEL["openrouter"]
            key = pipeline.vector_namespace("openrouter", model)
            fresh = pipeline.pack_vector([0., 1.])
            with contextlib.closing(pipeline.index_connection(root)) as db:
                db.executemany("INSERT INTO vectors VALUES ('same', ?, 2, ?)",
                               [(model, pipeline.pack_vector([1., 0.])), (key, fresh)])
                db.commit()
            with contextlib.closing(pipeline.index_connection(root)) as db:
                self.assertEqual(db.execute("SELECT vector FROM vectors WHERE model = ?", (key,)).fetchone()[0], fresh)

    def test_embedding_response_rejects_malformed_indices_and_shapes(self):
        for response in (
            [], {"data": [None]}, {"data": {"0": {"embedding": [1.]}}},
            *({"data": [{"index": value, "embedding": [1.]}]} for value in (False, 0.5, "0")),
            {"data": [{"index": 0, "embedding": [1e300]}]},
        ):
            with self.subTest(response=response), mock.patch.object(pipeline, "post_json", return_value=response):
                with self.assertRaises(RuntimeError):
                    pipeline.embed_texts_openrouter(["text"], "fake", "review-model")

    def test_local_embeddings_obey_the_same_response_validation(self):
        for vectors in ([], [[]], [[float("nan")]], [[True]]):
            with self.subTest(vectors=vectors), mock.patch.object(pipeline, "embed_texts_local", return_value=vectors):
                with self.assertRaises(RuntimeError):
                    pipeline.embed_texts(["text"], "", "local-model", "local")

    def test_incremental_embeddings_reject_contract_dimension_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "one", title="existing")
            pipeline.build_index(root)
            with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
                pipeline.build_embeddings(root, "fake", "review-model")
            seed_video(root, "two", title="new text")
            pipeline.build_index(root)
            with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0., 0.]]), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    pipeline.build_embeddings(root, "fake", "review-model")
            with contextlib.closing(pipeline.read_connection(root)) as db:
                self.assertEqual(db.execute("SELECT DISTINCT dim FROM vectors").fetchall(), [(2,)])

    def test_semantic_rejects_corrupt_cached_vector_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "one", title="topic")
            pipeline.build_index(root)
            key = pipeline.vector_namespace("openrouter", "review-model")
            for vector in ([1.], [float("nan"), 0.]):
                with contextlib.closing(pipeline.index_connection(root)) as db:
                    db.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?, 2, ?)",
                               (pipeline.text_hash("topic"), key, pipeline.pack_vector(vector)))
                    db.commit()
                with self.subTest(vector=vector), mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]):
                    with self.assertRaises(RuntimeError):
                        pipeline.semantic_search(root, "topic", "fake", "review-model", 10)
                    result = pipeline.hybrid_search(root, "topic", 10, embed_key="fake", embed_model="review-model", embed_backend="openrouter")
                    self.assertEqual(result["degraded_reason"], "embed_failed")
                    self.assertTrue(result["hits"])

    def test_vector_candidates_keep_memory_bounded_by_top_k(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with contextlib.closing(pipeline.index_connection(root)) as db:
                db.execute("INSERT INTO videos (job) VALUES ('one')")
                text = "x" * 1000
                digest = pipeline.text_hash(text)
                db.executemany("INSERT INTO occurrences VALUES (?, 'one', ?, 'speech', ?, NULL, 'point', ?, ?, 'v1')",
                               ((f"occ_{i:05d}", i, i, text, digest) for i in range(6000)))
                db.execute("INSERT INTO vectors VALUES (?, 'model', 2, ?)", (digest, pipeline.pack_vector([1., 0.])))
                tracemalloc.start()
                try:
                    hits = pipeline._vector_candidates(db, [1., 0.], "model", pipeline.SearchFilter(), 1)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertEqual(len(hits), 1)
                self.assertLess(peak, 2_000_000, "top-K must not retain the full corpus text")

    def test_context_does_not_merge_usable_blocks_past_budget(self):
        hits = [dict(job="one", kind="speech", occurrence_id=f"occ_{i}",
                     start=i * 10, end=(i + 1) * 10, text="x" * 100,
                     timestamp_kind="range") for i in range(2)]
        single = pipeline.pack_context(hits[:1], token_budget=1000)
        self.assertEqual(single["included"], 1)
        packed = pipeline.pack_context(hits, token_budget=single["est_tokens"] + 10)
        self.assertEqual(packed["included"], 1)
        self.assertEqual(packed["blocks"][0]["occurrence_ids"], ["occ_0"])
        self.assertEqual(packed["omitted_reasons"], {"token_budget": 1})

    def test_context_falls_back_to_individual_ranges_for_remaining_budget(self):
        hits = [dict(job=job, kind="speech", occurrence_id=name, start=start, end=end,
                     title="T", text="x" * size, timestamp_kind="range")
                for name, job, start, end, size in (
                    ("top", "a", 0, 1, 120), ("b1", "b", 0, 10, 60), ("b2", "b", 10, 20, 60)
                )]
        packed = pipeline.pack_context(hits, token_budget=300)
        self.assertEqual([block["occurrence_ids"] for block in packed["blocks"]], [["top"], ["b1"]])
        self.assertEqual(packed["omitted_reasons"], {"token_budget": 1})

    def test_single_video_ask_keeps_all_evidence_when_budget_allows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "one", timeline=[
                {"timestamp": i * 10, "type": "speech", "text": f"topic {i}"}
                for i in range(8)
            ])
            pipeline.build_index(root)
            with mock.patch.object(pipeline, "question_keywords", return_value=["topic"]), mock.patch.object(pipeline, "minimax_chat", return_value="影片提到 topic [1]。"):
                result = pipeline.answer_question(root, "question", "fake", "model", 12)
            self.assertEqual(result["packing"]["included"], 8)
            self.assertEqual(result["packing"]["omitted"], 0)

    def test_context_budget_accounts_for_metadata_and_question(self):
        hit = dict(job="one", kind="speech", occurrence_id="occ_1", start=1, end=None,
                   title="T" * 10000, text="x", timestamp_kind="point")
        self.assertEqual(pipeline.pack_context([hit], token_budget=100)["included"], 0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "one", title="topic")
            pipeline.build_index(root)
            with mock.patch.object(pipeline, "question_keywords", return_value=["topic"]), mock.patch.object(pipeline, "minimax_chat") as answer:
                result = pipeline.answer_question(root, "Q" * 10000, "fake", "model", 8, context_budget=100)
            self.assertTrue(result["insufficient_evidence"])
            answer.assert_not_called()

    def test_all_numeric_citation_indices_are_checked_without_int_overflow(self):
        answer = "Valid [1], invalid [1000], huge [" + "9" * 5000 + "]."
        cleaned, cited, dropped = pipeline.validate_answer_citations(answer, [{}])
        self.assertEqual(cited, [1])
        self.assertEqual(dropped, 2)
        self.assertNotIn("[1000]", cleaned)
        self.assertLess(len(cleaned), 100)

    def test_unicode_decimal_citations_remain_validated(self):
        cleaned, cited, dropped = pipeline.validate_answer_citations("valid [１], invalid [９].", [{}])
        self.assertEqual(cited, [1])
        self.assertEqual(dropped, 1)
        self.assertNotIn("[９]", cleaned)

    def test_uncited_model_answer_is_not_published_as_grounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            for answer in ("Unsupported assertion [999].", "No evidence supports this."):
                with self.subTest(answer=answer), mock.patch.object(pipeline, "question_keywords", return_value=["時間軸"]), mock.patch.object(pipeline, "minimax_chat", return_value=answer):
                    result = pipeline.answer_question(root, "question", "fake", "fake-model", 8)
                self.assertTrue(result["insufficient_evidence"])
                self.assertEqual(result["citations"], [])
                self.assertNotIn("Unsupported assertion", result["answer"])
                self.assertTrue(result["evidence"])

    def test_retrieval_cli_honors_offline_flags_before_cloud_or_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            for flag in ("--dry-run", "--local-only"):
                commands = [
                    ["--search", "topic", "--hybrid"],
                    ["--search", "topic", "--hybrid", "--embed-backend", "local", "--rerank"],
                    ["--ask", "question", "--hybrid"],
                    ["--rebuild-embeddings", "--allow-batch-cloud"],
                ]
                if flag == "--dry-run":
                    commands.append(["--rebuild-embeddings", "--embed-backend", "local"])
                for command in commands:
                    args = pipeline.parse_args(["--root", str(root), flag, *command])
                    with self.subTest(flag=flag, command=command), contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch.object(pipeline, "parse_args", return_value=args))
                        stack.enter_context(mock.patch.object(pipeline, "load_dotenv"))
                        stack.enter_context(mock.patch.object(pipeline, "minimax_subscription_key", return_value="fake"))
                        stack.enter_context(mock.patch.object(pipeline, "minimax_model", return_value="fake-model"))
                        stack.enter_context(mock.patch.dict(pipeline.os.environ, {"OPENROUTER_API_KEY": "fake"}))
                        calls = [stack.enter_context(mock.patch.object(pipeline, name)) for name in ("hybrid_search", "answer_question", "rebuild_embeddings")]
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                        self.assertEqual(pipeline.main(), 1)
                        for call in calls:
                            call.assert_not_called()

    def test_metadata_uses_rss_publication_and_never_creation_as_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = seed_video(root, "rss", title="topic")
            manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
            manifest["rss"] = {"published": "Wed, 02 Sep 2026 23:15:00 GMT"}
            pipeline.write_json(job / "manifest.json", manifest)
            pipeline.build_index(root)
            with contextlib.closing(pipeline.read_connection(root)) as db:
                dates = db.execute("SELECT published_at, first_completed_at FROM videos").fetchone()
            self.assertEqual(dates, ("2026-09-02", None))
            manifest["completed_at"] = "2026-09-03T00:15:00Z"
            self.assertEqual(pipeline.video_source_meta(job, manifest)["first_completed_at"], manifest["completed_at"])

    def test_unknown_date_exclusions_are_visible_in_json_and_ask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            args = pipeline.parse_args(["--root", str(root), "--search", "資安", "--since", "2026-01-01", "--json"])
            output = io.StringIO()
            with mock.patch.object(pipeline, "parse_args", return_value=args), mock.patch.object(pipeline, "load_dotenv"), contextlib.redirect_stdout(output):
                self.assertEqual(pipeline.main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["unknown_dates_excluded"], 1)
            self.assertEqual(payload["date_field"], "published_at")
            with mock.patch.object(pipeline, "question_keywords", return_value=["資安"]), mock.patch.object(pipeline, "minimax_chat", return_value="影片提到資安 [1]。"):
                result = pipeline.answer_question(root, "question", "fake", "fake-model", 8, filters=pipeline.SearchFilter(since="2026-01-01"))
            self.assertEqual(result["retrieval"]["unknown_dates_excluded"], 1)
            self.assertEqual(result["retrieval"]["date_field"], "published_at")

    def test_hybrid_degrades_on_non_runtime_embed_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            seed_vectors(root, pipeline.vector_namespace("openrouter", "test-embed"))
            for failure in (OSError("model cache unreadable"), ValueError("bad config")):
                with self.subTest(failure=type(failure).__name__), mock.patch.object(
                    pipeline, "embed_texts", side_effect=failure
                ):
                    result = pipeline.hybrid_search(
                        root, "資安", 10,
                        embed_key="k", embed_model="test-embed", embed_backend="openrouter",
                    )
                self.assertEqual(result["retrieval_mode"], "fts")
                self.assertEqual(result["degraded_reason"], "embed_failed")
            with mock.patch.object(pipeline, "embed_texts", side_effect=OSError("disk gone")):
                with self.assertRaises(OSError):
                    pipeline.hybrid_search(
                        root, "資安", 10,
                        embed_key="k", embed_model="test-embed",
                        embed_backend="openrouter", strict=True,
                    )

    def test_audit_counts_distinct_texts_and_ignores_stale_vectors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            same = [{"timestamp": 1.0, "type": "speech", "text": "同一段逐字稿"}]
            seed_video(root, "one", timeline=same)
            seed_video(root, "two", timeline=same)  # 同文：兩個 occurrence 共用一個向量
            pipeline.build_index(root)
            seed_vectors(root, pipeline.vector_namespace("openrouter", "m"))
            with contextlib.redirect_stdout(io.StringIO()):
                issues = pipeline.audit_library(root)
            self.assertFalse([i for i in issues if "向量" in i])
            # 一筆 stale 向量＋一種未嵌入的新文字：stale 不可掩蓋缺口
            with contextlib.closing(pipeline.index_connection(root)) as db:
                db.execute(
                    "INSERT INTO vectors VALUES ('stalehash', ?, 2, ?)",
                    (pipeline.vector_namespace("openrouter", "m"), pipeline.pack_vector([1.0, 0.0])),
                )
                db.commit()
            seed_video(root, "three", timeline=[{"timestamp": 0.0, "type": "speech", "text": "全新的句子"}])
            pipeline.build_index(root)
            with contextlib.redirect_stdout(io.StringIO()):
                issues = pipeline.audit_library(root)
            missing = [i for i in issues if "還沒有" in i]
            self.assertEqual(len(missing), 1)
            self.assertIn("1 種文字還沒有", missing[0])

    def test_cli_rejects_non_positive_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            for command in (
                ["--search", "資安", "--search-limit", "-1"],
                ["--search", "資安", "--search-limit", "0"],
                ["--ask", "問題", "--ask-limit", "0"],
            ):
                args = pipeline.parse_args(["--root", str(root), *command])
                stderr = io.StringIO()
                with self.subTest(command=command), contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(pipeline, "parse_args", return_value=args))
                    stack.enter_context(mock.patch.object(pipeline, "load_dotenv"))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(contextlib.redirect_stderr(stderr))
                    self.assertEqual(pipeline.main(), 1)
                self.assertIn("≥ 1", stderr.getvalue())

    def test_search_json_uses_machine_retrieval_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            args = pipeline.parse_args(["--root", str(root), "--search", "資安", "--json"])
            output = io.StringIO()
            with mock.patch.object(pipeline, "parse_args", return_value=args), mock.patch.object(pipeline, "load_dotenv"), contextlib.redirect_stdout(output):
                self.assertEqual(pipeline.main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["retrieval_mode"], "fts")
            self.assertIn("mode_label", payload)
            seed_vectors(root, pipeline.vector_namespace("openrouter", "test-embed"))
            args = pipeline.parse_args([
                "--root", str(root), "--search", "資安", "--json",
                "--semantic", "--embed-model", "test-embed",
            ])
            output = io.StringIO()
            with mock.patch.object(pipeline, "parse_args", return_value=args), mock.patch.object(pipeline, "load_dotenv"), mock.patch.dict(pipeline.os.environ, {"OPENROUTER_API_KEY": "k"}), mock.patch.object(pipeline, "embed_texts", side_effect=fake_embed_batch), contextlib.redirect_stdout(output):
                self.assertEqual(pipeline.main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["retrieval_mode"], "semantic")

    def test_min_similarity_filters_hybrid_vector_leg(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            model_key = pipeline.vector_namespace("openrouter", "test-embed")
            seed_vectors(root, model_key)
            with mock.patch.object(pipeline, "embed_texts", side_effect=fake_embed_batch):
                baseline = pipeline.hybrid_search(
                    root, "資安", 10,
                    embed_key="k", embed_model="test-embed", embed_backend="openrouter",
                )
            self.assertGreater(baseline["counts"]["vector"], 0)
            with mock.patch.object(pipeline, "embed_texts", side_effect=fake_embed_batch):
                result = pipeline.hybrid_search(
                    root, "資安", 10,
                    embed_key="k", embed_model="test-embed", embed_backend="openrouter",
                    min_similarity=2.0,  # 高於任何 cosine 分數：整條向量腿被門檻濾掉
                )
            self.assertEqual(result["counts"]["vector"], 0)
            self.assertEqual(result["retrieval_mode"], "hybrid")

    def test_citation_reports_max_end_across_merged_ranges(self):
        block = {
            "block_index": 1, "occurrence_ids": ["a", "b"], "job": "j", "kind": "speech",
            "title": "t", "source": "https://example.com/x", "timestamp_kind": "range",
            "ranges": [(0.0, 100.0), (50.0, 60.0)], "text": "text",
        }
        citation = pipeline._evidence_citation(block)
        self.assertEqual(citation["end"], 100.0)

    def test_rrf_fallback_key_does_not_merge_distinct_hits(self):
        fts = [dict(job="j", kind="speech", start=0.0, text="甲", occurrence_id=None)]
        vec = [dict(job="j", kind="speech", start=9.0, text="乙", occurrence_id=None)]
        fused = pipeline.rrf_fuse({"fts": fts, "vector": vec})
        self.assertEqual(len(fused), 2)
        self.assertEqual({h["text"] for h in fused}, {"甲", "乙"})
        same = dict(job="j", kind="speech", start=0.0, text="甲", occurrence_id=None)
        fused = pipeline.rrf_fuse({"fts": [same], "vector": [dict(same)]})
        self.assertEqual(len(fused), 1)

    def test_vector_candidates_dedupe_shared_text_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            same = [{"timestamp": 1.0, "type": "speech", "text": "同一段逐字稿"}]
            seed_video(root, "one", timeline=same)
            seed_video(root, "two", timeline=same)
            pipeline.build_index(root)
            model_key = pipeline.vector_namespace("openrouter", "test-embed")
            seed_vectors(root, model_key)
            with contextlib.closing(pipeline.read_connection(root)) as db:
                hits = pipeline._vector_candidates(
                    db, fake_embed("同一段逐字稿"), model_key, pipeline.SearchFilter(), 10
                )
            speech_hits = [h for h in hits if h["text"] == "同一段逐字稿"]
            self.assertEqual(len(speech_hits), 2)
            self.assertEqual({h["job"] for h in speech_hits}, {"one", "two"})
            self.assertEqual(speech_hits[0]["score"], speech_hits[1]["score"])

    def test_ask_explain_prints_rank_detail_for_citations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            seed_vectors(root, pipeline.vector_namespace("openrouter", "test-embed"))
            args = pipeline.parse_args([
                "--root", str(root), "--ask", "時間軸怎麼合併？", "--hybrid", "--explain",
                "--embed-backend", "openrouter", "--embed-model", "test-embed",
            ])
            output = io.StringIO()
            with mock.patch.object(pipeline, "parse_args", return_value=args), mock.patch.object(pipeline, "load_dotenv"), mock.patch.dict(pipeline.os.environ, {"OPENROUTER_API_KEY": "k"}), mock.patch.object(pipeline, "embed_texts", side_effect=fake_embed_batch), mock.patch.object(pipeline, "minimax_subscription_key", return_value="fake"), mock.patch.object(pipeline, "minimax_model", return_value="model"), mock.patch.object(pipeline, "minimax_chat", return_value="影片提到合併做法 [1]。"), contextlib.redirect_stdout(output):
                self.assertEqual(pipeline.main(), 0)
            self.assertIn("排序依據", output.getvalue())

    def test_empty_ask_fails_with_message_not_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = pipeline.parse_args(["--root", str(root), "--ask", ""])
            stderr = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(pipeline, "parse_args", return_value=args))
                stack.enter_context(mock.patch.object(pipeline, "load_dotenv"))
                run_pipeline = stack.enter_context(mock.patch.object(pipeline, "run_pipeline"))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                self.assertEqual(pipeline.main(), 1)
            self.assertIn("問題", stderr.getvalue())
            run_pipeline.assert_not_called()

    def test_date_field_is_visible_even_when_no_unknown_dates_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "one", title="topic", info={"upload_date": "20260801"})
            pipeline.build_index(root)
            for action in ("--search", "--ask"):
                args = pipeline.parse_args(["--root", str(root), action, "topic", "--since", "2026-01-01"])
                output = io.StringIO()
                with self.subTest(action=action), contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(pipeline, "parse_args", return_value=args))
                    stack.enter_context(mock.patch.object(pipeline, "load_dotenv"))
                    stack.enter_context(mock.patch.object(pipeline, "minimax_subscription_key", return_value="fake"))
                    stack.enter_context(mock.patch.object(pipeline, "minimax_model", return_value="model"))
                    stack.enter_context(mock.patch.object(pipeline, "question_keywords", return_value=["topic"]))
                    stack.enter_context(mock.patch.object(pipeline, "minimax_chat", return_value="影片提到 topic [1]。"))
                    stack.enter_context(contextlib.redirect_stdout(output))
                    self.assertEqual(pipeline.main(), 0)
                    self.assertIn("published_at", output.getvalue())


if __name__ == "__main__":
    unittest.main()
