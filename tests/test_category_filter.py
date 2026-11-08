import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def _write_job(root: Path, job: str, title: str, source: str, categories) -> None:
    job_dir = root / "jobs" / job
    job_dir.mkdir(parents=True)
    pipeline.write_json(job_dir / "manifest.json", {
        "status": "completed", "source": {"title": title, "url": source},
        "categories": categories,
    })
    pipeline.write_json(job_dir / "summary.json", {"short_summary": f"{title} 的摘要內容包含測試關鍵字"})


def _seed(root: Path) -> None:
    for job, title, source, cats in (
        ("job-ai", "AI 教學", "src-a", ["AI應用", "教學"]),
        ("job-ai-exact", "AI 精確分類", "src-e", ["AI"]),
        ("job-cat", "貓咪日常", "src-b", ["日常"]),
        ("job-none", "無分類片", "src-c", []),
        ("job-tricky", "誤匹配陷阱", "src-d", ["100%確定", "AI倫理", "AI_服務"]),
        ("job-percent", "百分號分類", "src-f", ["100%"]),
    ):
        _write_job(root, job, title, source, cats)
    pipeline.build_index(root)


class CategoryFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        _seed(self.root)

    def _seed_vectors(self) -> None:
        # Synthetic local embeddings exercise the current generation contract.
        with mock.patch.object(pipeline, "embed_texts", side_effect=lambda texts, *_a, **_k: [[1.0, 0.0] for _ in texts]):
            pipeline.build_embeddings(self.root, "", "test-model", "local")

    def _semantic(self, category: str):
        return pipeline.semantic_search(
            self.root, "query", "", "test-model", 30, "local",
            filters=pipeline.SearchFilter(category=category),
        )

    def test_category_requires_an_exact_json_string_element(self) -> None:
        for query in ("測試關鍵字", "測試"):
            with self.subTest(query=query):
                hits = pipeline.search_index(self.root, query, 30, "AI")
                self.assertEqual({h["job"] for h in hits}, {"job-ai-exact"})

    def test_percent_is_literal_and_category_still_requires_exact_match(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 30, "100%")
        self.assertEqual({h["job"] for h in hits}, {"job-percent"})

    def test_underscore_wildcard_escaped(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 30, "AI_")
        self.assertEqual({h["job"] for h in hits}, set())

    def test_no_category_returns_all(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 30)
        self.assertEqual(len(hits), 6)

    def test_category_membership_ignores_non_array_manifest_values(self) -> None:
        for job, categories in (
            ("scalar", "AI"),
            ("object-value", {"topic": "AI"}),
            ("object-key", {"AI": "elsewhere"}),
            ("null", None),
            ("nested", [None, 1, {"topic": "AI"}, ["AI"]]),
            ("valid-mixed", [None, 1, "AI"]),
            ("malformed", []),
        ):
            _write_job(self.root, job, job, "src-" + job, categories)
        pipeline.build_index(self.root)
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as connection, connection:
            connection.execute("UPDATE videos SET categories=? WHERE job='malformed'", ('["AI"',))
        self._seed_vectors()
        expected = {"job-ai-exact", "valid-mixed"}
        for query in ("測試關鍵字", "測試"):
            with self.subTest(mode="search", query=query):
                hits = pipeline.search_index(self.root, query, 30, "AI")
                self.assertEqual({hit["job"] for hit in hits}, expected)
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0]]):
            with self.subTest(mode="semantic"):
                self.assertEqual({hit["job"] for hit in self._semantic("AI")}, expected)
            for backend in ("", "local"):
                with (
                    self.subTest(mode="ask", backend=backend),
                    mock.patch.object(pipeline, "question_keywords", return_value=["測試關鍵字"]),
                    mock.patch.object(pipeline, "minimax_chat", return_value="{}"),
                ):
                    result = pipeline.answer_question(
                        self.root, "問題", "", "model", 30,
                        embed_model="test-model", embed_backend=backend,
                        filters=pipeline.SearchFilter(category="AI"),
                    )
                    self.assertEqual({hit["job"] for hit in result["citations"]}, expected)
        with self.subTest(mode="list"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.list_videos(self.root, "AI"), 2)
        with self.subTest(mode="dashboard"):
            page = pipeline.query_videos(self.root, filters={"category": "AI"}, readonly=True)
            self.assertEqual({row["job"] for row in page["items"]}, expected)
        for category in ("1", "AI' OR 1=1 --"):
            with self.subTest(category=category):
                self.assertEqual(pipeline.search_index(self.root, "測試關鍵字", 30, category), [])
                self.assertEqual(pipeline.query_videos(self.root, filters={"category": category}, readonly=True)["total"], 0)
        self.assertEqual(len(pipeline.search_index(self.root, "測試關鍵字", 30)), 13)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            pipeline.list_videos(self.root)
        self.assertIn("未分類", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertGreater(pipeline.list_videos(self.root, "未分類"), 0)

    def test_list_filter_uses_exact_category(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.list_videos(self.root, "AI"), 1)

    def test_semantic_search_category_filter(self) -> None:
        self._seed_vectors()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0]]):
            hits = self._semantic("AI")
        self.assertTrue(hits)
        self.assertEqual({h["job"] for h in hits}, {"job-ai-exact"})

    def test_semantic_category_miss_returns_no_hits(self) -> None:
        self._seed_vectors()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            self.assertEqual(self._semantic("不存在"), [])
            embed.assert_not_called()

    def test_semantic_ask_respects_exact_and_missing_categories(self) -> None:
        self._seed_vectors()
        for category, jobs in (("AI", {"job-ai-exact"}), ("不存在", set())):
            with (
                self.subTest(category=category),
                mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0]]),
                mock.patch.object(pipeline, "minimax_chat", return_value="{}") as ask,
                mock.patch.object(pipeline, "question_keywords") as keywords,
            ):
                result = pipeline.answer_question(
                    self.root, "問題", "", "model", 30, embed_model="test-model",
                    embed_backend="local", filters=pipeline.SearchFilter(category=category),
                )
                self.assertEqual({h["job"] for h in result["citations"]}, jobs)
                keywords.assert_not_called()
                if jobs:
                    ask.assert_called_once()
                else:
                    self.assertFalse(result["citations"])
                    ask.assert_not_called()

    def test_semantic_category_still_requires_model_vectors(self) -> None:
        with mock.patch.object(pipeline, "embed_texts") as embed:
            with self.assertRaisesRegex(RuntimeError, "先執行 `--embed`"):
                self._semantic("AI")
            embed.assert_not_called()

    def test_semantic_category_without_vectors_is_not_a_category_miss(self) -> None:
        self._seed_vectors()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as connection, connection:
            texts = [row[0] for row in connection.execute("SELECT text FROM events WHERE job=?", ("job-ai-exact",))]
            connection.executemany("DELETE FROM embedding_cache WHERE content_hash=?",
                                   [(pipeline.content_hash(text),) for text in texts])
        filters = pipeline.SearchFilter(category="AI")
        with (
            mock.patch.object(pipeline, "embed_texts") as embed,
            mock.patch.object(pipeline, "minimax_chat") as ask,
        ):
            with self.assertRaisesRegex(RuntimeError, "先執行 `--embed`"):
                self._semantic("AI")
            with self.assertRaisesRegex(RuntimeError, "semantic_unavailable"):
                pipeline.answer_question(self.root, "問題", "", "model", 30,
                                         embed_model="test-model", embed_backend="local",
                                         filters=filters, strict=True)
            embed.assert_not_called()
            ask.assert_not_called()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            result = pipeline.retrieve_evidence(self.root, "測試關鍵字", 30, filters=filters,
                                                mode="semantic", embed_backend="local", embed_model="test-model")
            embed.assert_not_called()
        self.assertEqual(result["retrieval"]["degraded_reason"], "semantic_unavailable")
        self.assertEqual({h["job"] for h in result["evidence"]}, {"job-ai-exact"})

    def test_semantic_cli_category_miss_succeeds(self) -> None:
        self._seed_vectors()
        for action in ("--search", "--ask"):
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(action=action),
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
                mock.patch("sys.argv", ["pipeline.py", "--root", str(self.root), action, "query", "--json",
                                       "--category", "不存在", "--semantic",
                                       "--embed-backend", "local", "--embed-model", "test-model"]),
                mock.patch.object(pipeline, "load_dotenv"),
                mock.patch.object(pipeline, "embed_texts") as embed,
                mock.patch.object(pipeline, "minimax_subscription_key", return_value=""),
                mock.patch.object(pipeline, "minimax_model", return_value="model"),
                mock.patch.object(pipeline, "minimax_chat") as ask,
            ):
                self.assertEqual(pipeline.main(), 0, errors.getvalue())
                self.assertEqual(json.loads(output.getvalue())["evidence"], [])
                embed.assert_not_called()
                ask.assert_not_called()

    def test_semantic_cli_missing_category_vectors_reports_fallback_or_strict_failure(self) -> None:
        self._seed_vectors()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as connection, connection:
            texts = [row[0] for row in connection.execute("SELECT text FROM events WHERE job=?", ("job-ai-exact",))]
            connection.executemany("DELETE FROM embedding_cache WHERE content_hash=?",
                                   [(pipeline.content_hash(text),) for text in texts])
        for strict in (False, True):
            output, errors = io.StringIO(), io.StringIO()
            argv = ["pipeline.py", "--root", str(self.root), "--search", "測試關鍵字",
                    "--category", "AI", "--semantic", "--embed-backend", "local", "--embed-model", "test-model"]
            if strict:
                argv.append("--search-strict")
            with (self.subTest(strict=strict), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
                  mock.patch("sys.argv", argv), mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}),
                  mock.patch.object(pipeline, "load_dotenv"), mock.patch.object(pipeline, "embed_texts") as embed):
                self.assertEqual(pipeline.main(), 1 if strict else 0)
                self.assertIn("semantic_unavailable", errors.getvalue() if strict else output.getvalue())
                if not strict:
                    self.assertIn("AI 精確分類", output.getvalue())
                embed.assert_not_called()

    def test_keyword_ask_passes_exact_category_to_retrieval(self) -> None:
        with (
            mock.patch.object(pipeline, "question_keywords", return_value=["測試關鍵字"]),
            mock.patch.object(pipeline, "minimax_chat", return_value="{}") as ask,
        ):
            result = pipeline.answer_question(
                self.root, "哪支影片提到這個？", "", "model", 30,
                filters=pipeline.SearchFilter(category="AI"),
            )
        self.assertEqual({hit["job"] for hit in result["citations"]}, {"job-ai-exact"})
        ask.assert_called_once()


if __name__ == "__main__":
    unittest.main()
