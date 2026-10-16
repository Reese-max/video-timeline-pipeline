import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _seed(self.root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.make_handler(self.root))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _get(
        self, path: str, host: str | None = None, origin: str | None = None
    ) -> urllib.request.addinfourl:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if host:
            req.add_header("Host", host)
        if origin:
            req.add_header("Origin", origin)
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
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method=method)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 405)
                self.assertEqual(ctx.exception.headers["X-Frame-Options"], "DENY")

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

    def test_search_preserves_legacy_vectors_and_database_bytes(self) -> None:
        connection = sqlite3.connect(self.root / "library.db")
        connection.execute("CREATE TABLE vectors (event_id INTEGER PRIMARY KEY, dim INTEGER, vector BLOB)")
        connection.execute("INSERT INTO vectors VALUES (1, 1, x'00')")
        connection.commit()
        connection.close()
        before = (self.root / "library.db").read_bytes()
        for query in ("安全關鍵字", "安全"):
            with self.subTest(query=query):
                body = self._get("/search?q=" + urllib.parse.quote(query)).read().decode()
                self.assertIn("測試影片", body)
                self.assertEqual((self.root / "library.db").read_bytes(), before)

    def test_readonly_db_encodes_filesystem_uri(self) -> None:
        special_root = self.root / "library #1 % 中文"
        special_root.mkdir()
        _seed(special_root)
        connection = webapp._readonly_db(special_root)
        try:
            self.assertEqual(
                Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve(),
                (special_root / "library.db").resolve(),
            )
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden (value TEXT)")
        finally:
            connection.close()
        self.assertFalse((self.root / "library ").exists())

    def test_incompatible_schema_reports_migration_without_writing(self) -> None:
        connection = sqlite3.connect(self.root / "library.db")
        connection.executescript(
            "DROP TABLE videos; CREATE TABLE videos (job TEXT PRIMARY KEY, title TEXT);"
            "INSERT INTO videos VALUES ('job1', 'legacy title');"
        )
        connection.close()
        before = (self.root / "library.db").read_bytes()
        for path in ("/", "/video/job1", "/search?q=legacy"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._get(path)
                self.assertEqual(ctx.exception.code, 503)
                body = ctx.exception.read().decode()
                self.assertIn("migration_required", body)
                self.assertNotIn(str(self.root), body)
                self.assertEqual((self.root / "library.db").read_bytes(), before)

    def test_foreign_origin_rejected_before_search(self) -> None:
        with mock.patch.object(webapp, "_search") as search:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get("/search?q=test", origin="https://evil.example")
            self.assertEqual(ctx.exception.code, 403)
            search.assert_not_called()
        self.assertEqual(self._get("/", origin=f"http://127.0.0.1:{self.port}").status, 200)

    def test_ipv6_and_invalid_host_headers(self) -> None:
        self.assertEqual(self._get("/", host=f"[::1]:{self.port}").status, 200)
        for host in ("localhost.evil", "127.0.0.1:bad", "localhost:65536", "[::1]evil"):
            with self.subTest(host=host):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._get("/", host=host)
                self.assertEqual(ctx.exception.code, 403)

    def test_serve_refuses_remote_before_binding(self) -> None:
        for host in ("0.0.0.0", "192.0.2.1", "::", "example.com"):
            with self.subTest(host=host), mock.patch.object(webapp, "ThreadingHTTPServer") as server:
                with self.assertRaisesRegex(ValueError, "loopback"):
                    webapp.serve(self.root, host, 0)
                server.assert_not_called()

    @unittest.skipUnless(socket.has_ipv6, "IPv6 unavailable")
    def test_serve_accepts_ipv6_loopback(self) -> None:
        with mock.patch.object(ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
            try:
                webapp.serve(self.root, "::1", 0)
            except KeyboardInterrupt:
                self.fail("serve must handle Ctrl+C")

    def test_serve_closes_socket_on_interrupt(self) -> None:
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 8080)
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(webapp, "ThreadingHTTPServer", return_value=server):
            try:
                webapp.serve(self.root)
            except KeyboardInterrupt:
                self.fail("serve must handle Ctrl+C")
        server.server_close.assert_called_once_with()

    def test_health_is_available_without_index(self) -> None:
        (self.root / "library.db").unlink()
        response = self._get("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.load(response), {"status": "ok"})
        self.assertFalse((self.root / "library.db").exists())

    def test_missing_index_reports_safe_maintenance_state(self) -> None:
        (self.root / "library.db").unlink()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/")
        self.assertEqual(ctx.exception.code, 503)
        body = ctx.exception.read().decode()
        self.assertIn("index_required", body)
        self.assertNotIn(str(self.root), body)
        self.assertFalse((self.root / "library.db").exists())

    def test_malformed_request_target_returns_400(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", "http://[", headers={"Host": f"127.0.0.1:{self.port}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        finally:
            connection.close()

    def test_video_links_round_trip_known_job_ids(self) -> None:
        job = "中文 job#?%"
        connection = sqlite3.connect(self.root / "library.db")
        connection.execute("INSERT INTO videos (job, title, categories) VALUES (?, 'encoded job', '[]')", (job,))
        connection.commit()
        connection.close()
        path = "/video/" + urllib.parse.quote(job, safe="")
        self.assertIn(path, self._get("/").read().decode())
        self.assertIn("encoded job", self._get(path).read().decode())

    def test_cli_serves_requested_root_without_api_keys(self) -> None:
        # Keep the port reserved until just before starting the CLI subprocess.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = {key: value for key, value in os.environ.items() if not key.endswith(("_KEY", "_TOKEN"))}
        process = subprocess.Popen(
            [sys.executable, str(Path(webapp.__file__).with_name("pipeline.py")),
             "--serve", "--root", str(self.root), "--port", str(port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), "CLI exited before serving HTTP")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                        self.assertIn("測試影片", response.read().decode())
                    break
                except urllib.error.URLError:
                    time.sleep(0.02)
            else:
                self.fail("CLI did not start within 10 seconds")
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
