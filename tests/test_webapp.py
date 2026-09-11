import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import webapp


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
        """
    )
    connection.execute(
        "INSERT INTO videos (job, title, source, categories) VALUES ('job1', '測試影片<script>', 'u', ?)",
        (json.dumps(["AI應用"]),),
    )
    connection.execute(
        "INSERT INTO events (job, timestamp, kind, text) VALUES ('job1', '00:00:05', 'summary', '含有安全關鍵字的摘要')"
    )
    connection.commit()
    connection.close()


class WebDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        _seed(cls.root)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.make_handler(cls.root))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.temporary.cleanup()

    def _get(self, path: str, host: str | None = None) -> urllib.request.addinfourl:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if host:
            req.add_header("Host", host)
        return urllib.request.urlopen(req, timeout=5)

    def test_index_lists_videos_and_escapes(self) -> None:
        body = self._get("/").read().decode()
        self.assertIn("測試影片", body)
        self.assertIn("&lt;script&gt;", body)  # XSS 已 escape
        self.assertNotIn("<script>", body)

    def test_search_uses_local_index_only(self) -> None:
        body = self._get("/search?q=%E5%AE%89%E5%85%A8%E9%97%9C%E9%8D%B5%E5%AD%97").read().decode()
        self.assertIn("測試影片", body)

    def test_video_detail(self) -> None:
        body = self._get("/video/job1").read().decode()
        self.assertIn("含有安全關鍵字的摘要", body)
        self.assertIn("AI應用", body)

    def test_unknown_path_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_post_rejected(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", data=b"x=1", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 405)

    def test_foreign_host_header_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/", host="evil.example.com")
        self.assertEqual(ctx.exception.code, 403)

    def test_security_headers_present(self) -> None:
        headers = self._get("/").headers
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))

    def test_db_opened_readonly(self) -> None:
        # mode=ro：dashboard 無法寫入，連誤觸都不能
        connection = webapp._readonly_db(self.root)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO videos (job) VALUES ('hack')")
        connection.close()


if __name__ == "__main__":
    unittest.main()
