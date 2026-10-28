import io
import json
import os
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import check_youtube_proxy as health


def run_probe(healthy, args=None):
    with patch.object(health, "probe", return_value=healthy):
        return health.main(args or [])


class ConfiguredProxyHealthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "health.json"
        self.enterContext(patch.dict(os.environ, {
            "YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080",
            "YOUTUBE_PROXY_HEALTH_STATE": str(self.state),
        }, clear=True))
        self.enterContext(patch.object(health.pipeline, "load_dotenv"))
        self.notify = self.enterContext(patch.object(
            health, "notify", return_value={"stage": "send", "status": "accepted"},
        ))

    def test_threshold_and_recovery(self):
        state, notify = self.state, self.notify
        self.assertEqual(run_probe(True), 0)
        self.assertEqual(run_probe(False), 1)
        notify.assert_not_called()
        self.assertEqual(json.loads(state.read_text())["failures"], 1)
        self.assertEqual(run_probe(False), 1)
        self.assertEqual(notify.call_args.args[1], "unhealthy")
        self.assertEqual(run_probe(False), 1)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(run_probe(True), 0)
        self.assertEqual(notify.call_args.args[1], "healthy")
        self.assertEqual(run_probe(True), 0)
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(json.loads(state.read_text()), {
            "status": "healthy", "failures": 0,
            "notification": {"stage": "send", "status": "accepted"},
        })

    def test_initial_failure_custom_threshold_and_reset(self):
        with patch.dict(os.environ, {"YOUTUBE_PROXY_HEALTH_FAILURES": "3"}):
            for result in [False, False, True, False, False]:
                run_probe(result)
            self.notify.assert_not_called()
            run_probe(False)
            self.assertEqual(self.notify.call_count, 1)

    def test_not_configured(self):
        with patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": ""}), patch.object(health, "probe") as probe:
            self.assertEqual(health.main([]), 2)
        probe.assert_not_called()
        self.notify.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_dotenv_uses_app_loader(self):
        def load(path):
            self.assertEqual(path, health.REPO / ".env")
            os.environ["YTDLP_YOUTUBE_PROXY"] = "http://from-dotenv.test:1234"
        with patch.object(health.pipeline, "load_dotenv", side_effect=load), patch.object(health, "probe", return_value=True) as probe:
            self.assertEqual(health.main([]), 0)
        probe.assert_called_once_with("http://from-dotenv.test:1234")

    def test_dry_run_preserves_state(self):
        self.state.write_text(json.dumps({"status": "healthy", "failures": 1}))
        before = self.state.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_probe(False, ["--dry-run"]), 1)
        self.assertIn("將通知狀態變更為 unhealthy", output.getvalue())
        self.assertEqual(self.state.read_bytes(), before)
        self.notify.assert_not_called()

    def test_missing_state_parents_are_created(self):
        state = self.root / "missing" / "nested" / "health.json"
        with patch.dict(os.environ, {"YOUTUBE_PROXY_HEALTH_STATE": str(state)}):
            self.assertFalse(state.parent.exists())
            self.assertEqual(run_probe(False), 1)
            self.assertEqual(json.loads(state.read_text())["failures"], 1)
            self.assertEqual(run_probe(False), 1)
            self.assertEqual(json.loads(state.read_text())["status"], "unhealthy")
        self.notify.assert_called_once()


class YoutubeProxyHealthTests(unittest.TestCase):
    def test_notification_failure_is_logged_and_persisted(self):
        from email_providers import FakeProvider
        for target, stage in [("get_configured_provider", "provider"),
                              ("get_operations_db", "outbox"),
                              ("claim_pending_deliveries", "claim"),
                              ("update_delivery_status", "delivery_audit")]:
            with self.subTest(target=target, stage=stage), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "health.json"
                env = {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080", "YOUTUBE_PROXY_HEALTH_STATE": str(state),
                       "YOUTUBE_PROXY_HEALTH_FAILURES": "1", "DISCORD_WEBHOOK_URL": "https://example.test/hook"}
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr, \
                        patch.dict(os.environ, env, clear=True), patch.object(health.pipeline, "load_dotenv"), \
                        patch.object(health.notifications, "bind_cost_root"), \
                        patch.object(health.notifications, "get_configured_provider", return_value=FakeProvider()), \
                        patch.object(health.notifications, target, side_effect=RuntimeError("secret")):
                    self.assertEqual(run_probe(False), 1)
                    diagnostic = {"stage": stage, "status": "error", "error_type": "RuntimeError"}
                    self.assertEqual(json.loads(state.read_text())["notification"], diagnostic)
                    # No transition: keep the last failure visible and do not retry the send.
                    self.assertEqual(run_probe(False), 1)
                    self.assertEqual(json.loads(state.read_text())["notification"], diagnostic)
                    self.assertEqual(run_probe(True), 0)
                self.assertIn(stage, stderr.getvalue())
                self.assertIn("RuntimeError", stderr.getvalue())
                self.assertNotIn("secret", stdout.getvalue() + stderr.getvalue() + state.read_text())

    def test_absent_notification_config_is_safe(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output, \
                patch.dict(os.environ, {}, clear=True), patch.object(health.notifications, "get_operations_db") as db:
            self.assertEqual(health.notify(Path(directory), "unhealthy", "transition"), {
                "stage": "configuration", "status": "skipped",
            })
        self.assertIn("略過通知", output.getvalue())
        db.assert_not_called()

    def test_tcp_connect_and_http_connect(self):
        for chunks, expected in [([b"HTTP/1.1 200 Connection", b" established\r\n\r\n"], True),
                                 ([b"HTTP/1.1 407 Auth required\r\n\r\n"], False),
                                 ([b"HTTP/1.1 200 OK\r\n", b""], False),
                                 ([b"garbage 200 OK\r\n\r\n"], False), ([b"x" * 8192], False)]:
            with self.subTest(chunks=chunks, expected=expected):
                conn = MagicMock()
                conn.__enter__.return_value = conn
                conn.recv.side_effect = chunks
                with patch.object(health.socket, "create_connection", return_value=conn) as connect:
                    self.assertIs(health.probe("http://user:secret@proxy.test:8080"), expected)
                connect.assert_called_once_with(("proxy.test", 8080), timeout=3)
                request = conn.sendall.call_args.args[0]
                self.assertIn(b"CONNECT www.youtube.com:443 HTTP/1.1\r\n", request)
                self.assertIn(b"Proxy-Authorization: Basic ", request)

    def test_socket_timeout(self):
        with patch.object(health.socket, "create_connection", side_effect=TimeoutError):
            self.assertFalse(health.probe("http://proxy.test:8080"))

    def test_invalid_proxy(self):
        for proxy in ["socks5://proxy.test", "http://proxy.test:bad", "not-a-proxy"]:
            with self.subTest(proxy=proxy), patch.object(health.socket, "create_connection") as connect:
                self.assertFalse(health.probe(proxy))
            connect.assert_not_called()

    def test_notification_reuses_outbox(self):
        from email_providers import FakeProvider
        for provider_name, settings in [
            ("webhook", {"DISCORD_WEBHOOK_URL": "https://example.test/hook"}),
            ("smtp", {"EMAIL_PROVIDER": "smtp", "EMAIL_TO": "test@example.test", "EMAIL_FROM": "from@example.test", "SMTP_HOST": "smtp.test"}),
            ("resend", {"EMAIL_PROVIDER": "resend", "EMAIL_TO": "test@example.test", "EMAIL_FROM": "from@example.test", "RESEND_API_KEY": "test-key"}),
        ]:
            with self.subTest(provider_name=provider_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                n = health.notifications
                provider = FakeProvider()
                with patch.dict(os.environ, settings, clear=True), \
                        patch.object(n, "get_configured_provider", autospec=True, return_value=provider) as factory, \
                        patch.object(n, "bind_cost_root"):
                    self.assertEqual(health.notify(root, "unhealthy", "transition"), {"stage": "send", "status": "accepted"})
                factory.assert_called_once_with(provider_name)
                self.assertEqual(len(provider.history), 1)
                with closing(n.get_operations_db(root)) as conn:
                    delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
                    self.assertEqual(delivery["status"], "accepted")
                    self.assertEqual(provider.history[0]["idempotency_key"], delivery["provider_idempotency_key"])
                    self.assertEqual(conn.execute("SELECT outcome FROM delivery_attempts").fetchone()[0], "accepted")

    def test_provider_send_outcome_is_recorded(self):
        from email_providers import FakeProvider
        for outcome in ["accepted", "retryable_not_accepted", "permanent_failure", "unknown", "exception"]:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provider = FakeProvider(mode=outcome if outcome != "exception" else "unknown",
                                        crash_before_return=outcome == "exception", safe_error="secret")
                state = root / "health.json"
                env = {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080", "YOUTUBE_PROXY_HEALTH_STATE": str(state),
                       "YOUTUBE_PROXY_HEALTH_FAILURES": "1", "DISCORD_WEBHOOK_URL": "https://example.test/hook"}
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr, \
                        patch.dict(os.environ, env, clear=True), patch.object(health.pipeline, "load_dotenv"), \
                        patch.object(health.notifications, "get_configured_provider", return_value=provider), \
                        patch.object(health.notifications, "bind_cost_root"):
                    self.assertEqual(run_probe(False), 1)
                    expected = {"stage": "send", "status": "unknown" if outcome == "exception" else outcome}
                    if outcome == "exception":
                        expected["error_type"] = "OSError"
                    self.assertEqual(json.loads(state.read_text())["notification"], expected)
                    self.assertEqual(run_probe(False), 1)
                    self.assertEqual(len(provider.history), 1)
                with closing(health.notifications.get_operations_db(root)) as conn:
                    self.assertEqual(conn.execute("SELECT outcome FROM delivery_attempts").fetchone()[0], expected["status"])
                if outcome != "accepted":
                    self.assertIn("send", stderr.getvalue())
                if outcome == "exception":
                    self.assertIn("OSError", stderr.getvalue())
                self.assertNotIn("secret", stdout.getvalue() + stderr.getvalue() + state.read_text())


if __name__ == "__main__":
    unittest.main()
