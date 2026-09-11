import json
import sqlite3
import tempfile
import unittest
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
        ("job-cat", "貓咪日常", "src-b", '["日常"]'),
        ("job-none", "無分類片", "src-c", "[]"),
        # 陷阱列：categories 的 JSON 文字本身包含 'AI' 以外可被誤匹配的部分
        ("job-tricky", "誤匹配陷阱", "src-d", '["100%確定", "AI倫理"]'),
    ]
    for job, title, source, cats in videos:
        connection.execute(
            "INSERT INTO videos (job, title, source, status, categories) VALUES (?, ?, ?, 'done', ?)",
            (job, title, source, cats),
        )
        connection.execute(
            "INSERT INTO events (job, timestamp, kind, text) VALUES (?, ?, 'summary', ?)",
            (job, "00:00:10", f"{title} 的摘要內容包含測試關鍵字"),
        )
    connection.commit()
    connection.close()


class CategoryFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        _seed(self.root)

    def test_membership_scoped_to_json_elements(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 10, "AI")
        jobs = {h["job"] for h in hits}
        self.assertIn("job-ai", jobs)
        self.assertIn("job-tricky", jobs)  # 'AI倫理' 元素包含 AI 子字串
        self.assertNotIn("job-cat", jobs)

    def test_like_wildcards_in_category_are_escaped(self) -> None:
        # 舊行為：LIKE '%100%%' — 內建 % 變萬用字元會命中所有列
        hits = pipeline.search_index(self.root, "測試關鍵字", 10, "100%")
        self.assertEqual({h["job"] for h in hits}, {"job-tricky"})

    def test_underscore_wildcard_escaped(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 10, "AI_")
        self.assertEqual({h["job"] for h in hits}, set())

    def test_no_category_returns_all(self) -> None:
        hits = pipeline.search_index(self.root, "測試關鍵字", 10)
        self.assertEqual(len(hits), 4)

    def test_semantic_search_category_filter(self) -> None:
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
        with unittest.mock.patch.object(pipeline, "embed_texts", return_value=[[1.0, 0.0, 0.0, 0.0]]):
            hits = pipeline.semantic_search(
                self.root, "query", "key", "test-model", 10, backend="x", category="AI"
            )
        self.assertTrue(hits)
        self.assertTrue(all(h["job"] in {"job-ai", "job-tricky"} for h in hits))


if __name__ == "__main__":
    unittest.main()
