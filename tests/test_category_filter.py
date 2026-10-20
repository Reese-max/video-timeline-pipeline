import contextlib
import io
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import pipeline


def _seed(root: Path) -> None:
    connection = sqlite3.connect(root / "library.db")
    connection.executescript(
        """
        CREATE TABLE videos (
            job TEXT PRIMARY KEY, title TEXT, source TEXT, status TEXT,
            duration REAL, indexed_at TEXT, categories TEXT
        );
        CREATE VIRTUAL TABLE events USING fts5(
            job UNINDEXED, timestamp UNINDEXED, kind UNINDEXED, text,
            tokenize='trigram'
        );
        CREATE TABLE vectors (text_hash TEXT, model TEXT, dim INTEGER, vector BLOB, PRIMARY KEY (text_hash, model));
        CREATE TABLE index_state (job TEXT PRIMARY KEY, signature TEXT NOT NULL, row_count INTEGER NOT NULL);
        """
    )
    videos = [
        ("job-ai", "AI 教學", "src-a", '["AI應用", "教學"]'),
        ("job-ai-exact", "AI 精確分類", "src-e", '["AI"]'),
        ("job-cat", "貓咪日常", "src-b", '["日常"]'),
        ("job-none", "無分類片", "src-c", "[]"),
        # Partial labels and LIKE punctuation must not turn into category matches.
        ("job-tricky", "誤匹配陷阱", "src-d", '["100%確定", "AI倫理", "AI_服務"]'),
        ("job-percent", "百分號分類", "src-f", '["100%"]'),
    ]
    for job, title, source, cats in videos:
        connection.execute(
            "INSERT INTO videos (job, title, source, status, categories) VALUES (?, ?, ?, 'done', ?)",
            (job, title, source, cats),
        )
        connection.execute(
            "INSERT INTO events (job, timestamp, kind, text) VALUES (?, ?, 'summary', ?)",
            (job, 10, f"{title} 的摘要內容包含測試關鍵字"),
        )
    connection.commit()
    connection.close()


class CategoryFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        _seed(self.root)

    def test_category_requires_an_exact_json_string_element(self) -> None:
        for query in ("測試關鍵字", "測試"):
            with self.subTest(query=query):
                hits = pipeline.search_index(self.root, query, 10, "AI")
                self.assertEqual({h["job"] for h in hits}, {"job-ai-exact"})

    def test_percent_is_literal_and_category_still_requires_exact_match(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 10, "100%")
        self.assertEqual({h["job"] for h in hits}, {"job-percent"})

    def test_underscore_wildcard_escaped(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 10, "AI_")
        self.assertEqual({h["job"] for h in hits}, set())

    def test_no_category_returns_all(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 10)
        self.assertEqual(len(hits), 6)

    def test_list_filter_uses_exact_category(self) -> None:
        self.assertEqual(pipeline.list_videos(self.root, "AI"), 1)

    def _seed_vectors(self) -> None:
        # 假向量：直接 seed vectors 表並 mock embed_texts
        connection = sqlite3.connect(self.root / "library.db")
        dim = 4
        blob = pipeline.pack_vector([1.0, 0.0, 0.0, 0.0]) if hasattr(pipeline, "pack_vector") else b"\x00" * dim * 4
        # 讓每列事件的 text 都有向量
        for (text,) in connection.execute("SELECT DISTINCT text FROM events"):
            connection.execute(
                "INSERT INTO vectors (text_hash, model, dim, vector) VALUES (?, 'test-model', ?, ?)",
                (pipeline.text_hash(text), dim, blob),
            )
        connection.commit()
        connection.close()

    def test_semantic_search_category_filter(self) -> None:
        self._seed_vectors()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]):
            hits = pipeline.semantic_search(
                self.root, "query", "key", "test-model", 10, backend="x", category="AI"
            )
        self.assertTrue(hits)
        self.assertEqual({h["job"] for h in hits}, {"job-ai-exact"})

    def test_semantic_category_miss_returns_no_hits(self) -> None:
        self._seed_vectors()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]):
            self.assertEqual(pipeline.semantic_search(
                self.root, "query", "key", "test-model", 10, category="不存在"
            ), [])

    def test_semantic_ask_respects_exact_and_missing_categories(self) -> None:
        self._seed_vectors()
        for category, jobs in (("AI", {"job-ai-exact"}), ("不存在", set())):
            with (
                self.subTest(category=category),
                mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]),
                mock.patch.object(pipeline, "minimax_chat", return_value="答案 [1]") as ask,
                mock.patch.object(pipeline, "question_keywords") as keywords,
            ):
                result = pipeline.answer_question(
                    self.root, "問題", "key", "model", 10,
                    embed_model="test-model", embed_backend="local", category=category,
                )
                self.assertEqual({h["job"] for h in result["citations"]}, jobs)
                keywords.assert_not_called()
                if jobs:
                    ask.assert_called_once()
                else:
                    self.assertEqual(result["answer"], "索引裡找不到相關片段。")
                    ask.assert_not_called()

    def test_semantic_category_still_requires_model_vectors(self) -> None:
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]):
            with self.assertRaisesRegex(RuntimeError, "先執行 `--embed`"):
                pipeline.semantic_search(
                    self.root, "query", "key", "test-model", 10, category="AI"
                )

    def test_semantic_category_without_vectors_is_not_a_category_miss(self) -> None:
        self._seed_vectors()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as connection:
            text = connection.execute("SELECT text FROM events WHERE job = 'job-ai-exact'").fetchone()[0]
            connection.execute("DELETE FROM vectors WHERE text_hash = ?", (pipeline.text_hash(text),))
            connection.commit()
        with (
            mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]),
            mock.patch.object(pipeline, "minimax_chat") as ask,
        ):
            with self.assertRaisesRegex(RuntimeError, "先執行 `--embed`"):
                pipeline.semantic_search(
                    self.root, "query", "key", "test-model", 10, category="AI"
                )
            with self.assertRaisesRegex(RuntimeError, "先執行 `--embed`"):
                pipeline.answer_question(
                    self.root, "問題", "key", "model", 10,
                    embed_model="test-model", embed_backend="local", category="AI",
                )
            ask.assert_not_called()

    def test_semantic_cli_category_miss_succeeds(self) -> None:
        self._seed_vectors()
        for action, expected in (("--search", "命中 0 筆"), ("--ask", "索引裡找不到相關片段。")):
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(action=action),
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
                mock.patch("sys.argv", ["pipeline.py", "--root", str(self.root), action, "query",
                                       "--category", "不存在", "--semantic",
                                       "--embed-backend", "local", "--embed-model", "test-model"]),
                mock.patch.object(pipeline, "load_dotenv"),
                mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]),
                mock.patch.object(pipeline, "minimax_subscription_key", return_value="key"),
                mock.patch.object(pipeline, "minimax_model", return_value="model"),
                mock.patch.object(pipeline, "minimax_chat") as ask,
            ):
                self.assertEqual(pipeline.main(), 0, errors.getvalue())
                self.assertIn(expected, output.getvalue())
                ask.assert_not_called()

    def test_keyword_ask_passes_exact_category_to_retrieval(self) -> None:
        with (
            mock.patch.object(pipeline, "question_keywords", return_value=["測試關鍵字"]),
            mock.patch.object(pipeline, "minimax_chat", return_value="答案 [1]") as ask,
        ):
            result = pipeline.answer_question(
                self.root, "哪支影片提到這個？", "key", "model", 10, category="AI"
            )

        self.assertEqual({hit["job"] for hit in result["citations"]}, {"job-ai-exact"})
        ask.assert_called_once()


if __name__ == "__main__":
    unittest.main()
