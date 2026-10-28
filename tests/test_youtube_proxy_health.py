import json
import os
from unittest.mock import MagicMock, patch

import pytest
from scripts import check_youtube_proxy as health


@pytest.fixture
def configured(tmp_path):
    state = tmp_path / "health.json"
    with patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080",
                                 "YOUTUBE_PROXY_HEALTH_STATE": str(state)}, clear=True), \
            patch.object(health.pipeline, "load_dotenv"), \
            patch.object(health, "notify", return_value={"stage": "send", "status": "accepted"}) as notify:
        yield state, notify


def run_probe(healthy, args=None):
    with patch.object(health, "probe", return_value=healthy):
        return health.main(args or [])


def test_threshold_and_recovery(configured):
    state, notify = configured
    assert run_probe(True) == 0
    assert run_probe(False) == 1
    notify.assert_not_called()
    assert json.loads(state.read_text())["failures"] == 1
    assert run_probe(False) == 1
    assert notify.call_args.args[1] == "unhealthy"
    assert run_probe(False) == 1
    assert notify.call_count == 1
    assert run_probe(True) == 0
    assert notify.call_args.args[1] == "healthy"
    assert run_probe(True) == 0
    assert notify.call_count == 2
    assert json.loads(state.read_text()) == {"status": "healthy", "failures": 0,
                                           "notification": {"stage": "send", "status": "accepted"}}


def test_initial_failure_custom_threshold_and_reset(configured):
    _, notify = configured
    with patch.dict(os.environ, {"YOUTUBE_PROXY_HEALTH_FAILURES": "3"}):
        for result in [False, False, True, False, False]:
            run_probe(result)
        notify.assert_not_called()
        run_probe(False)
        assert notify.call_count == 1


def test_not_configured(configured):
    state, notify = configured
    with patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": ""}), patch.object(health, "probe") as probe:
        assert health.main([]) == 2
    probe.assert_not_called()
    notify.assert_not_called()
    assert not state.exists()


def test_dotenv_uses_app_loader(configured):
    def load(path):
        assert path == health.REPO / ".env"
        os.environ["YTDLP_YOUTUBE_PROXY"] = "http://from-dotenv.test:1234"
    with patch.object(health.pipeline, "load_dotenv", side_effect=load), patch.object(health, "probe", return_value=True) as probe:
        assert health.main([]) == 0
    probe.assert_called_once_with("http://from-dotenv.test:1234")


def test_dry_run_preserves_state(configured, capsys):
    state, notify = configured
    state.write_text(json.dumps({"status": "healthy", "failures": 1}))
    before = state.read_bytes()
    assert run_probe(False, ["--dry-run"]) == 1
    assert "將通知狀態變更為 unhealthy" in capsys.readouterr().out
    assert state.read_bytes() == before
    notify.assert_not_called()


def test_missing_state_parents_are_created(configured, tmp_path):
    _, notify = configured
    state = tmp_path / "missing" / "nested" / "health.json"
    with patch.dict(os.environ, {"YOUTUBE_PROXY_HEALTH_STATE": str(state)}):
        assert not state.parent.exists()
        assert run_probe(False) == 1
        assert json.loads(state.read_text())["failures"] == 1
        assert run_probe(False) == 1
        assert json.loads(state.read_text())["status"] == "unhealthy"
    notify.assert_called_once()


@pytest.mark.parametrize("target,stage", [("get_configured_provider", "provider"),
                                        ("get_operations_db", "outbox"),
                                        ("claim_pending_deliveries", "claim"),
                                        ("update_delivery_status", "delivery_audit")])
def test_notification_failure_is_logged_and_persisted(tmp_path, capsys, target, stage):
    from email_providers import FakeProvider
    state = tmp_path / "health.json"
    env = {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080", "YOUTUBE_PROXY_HEALTH_STATE": str(state),
           "YOUTUBE_PROXY_HEALTH_FAILURES": "1", "DISCORD_WEBHOOK_URL": "https://example.test/hook"}
    with patch.dict(os.environ, env, clear=True), patch.object(health.pipeline, "load_dotenv"), \
            patch.object(health.notifications, "bind_cost_root"), \
            patch.object(health.notifications, "get_configured_provider", return_value=FakeProvider()), \
            patch.object(health.notifications, target, side_effect=RuntimeError("secret")):
        assert run_probe(False) == 1
        diagnostic = {"stage": stage, "status": "error", "error_type": "RuntimeError"}
        assert json.loads(state.read_text())["notification"] == diagnostic
        # No transition: keep the last failure visible and do not retry the send.
        assert run_probe(False) == 1
        assert json.loads(state.read_text())["notification"] == diagnostic
        assert run_probe(True) == 0
    output = capsys.readouterr()
    assert stage in output.err and "RuntimeError" in output.err
    assert "secret" not in output.out + output.err + state.read_text()


def test_absent_notification_config_is_safe(tmp_path, capsys):
    with patch.dict(os.environ, {}, clear=True), patch.object(health.notifications, "get_operations_db") as db:
        assert health.notify(tmp_path, "unhealthy", "transition") == {"stage": "configuration", "status": "skipped"}
    assert "略過通知" in capsys.readouterr().out
    db.assert_not_called()


@pytest.mark.parametrize("chunks,expected", [([b"HTTP/1.1 200 Connection", b" established\r\n\r\n"], True),
    ([b"HTTP/1.1 407 Auth required\r\n\r\n"], False), ([b"HTTP/1.1 200 OK\r\n", b""], False),
    ([b"garbage 200 OK\r\n\r\n"], False), ([b"x" * 8192], False)])
def test_tcp_connect_and_http_connect(chunks, expected):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.recv.side_effect = chunks
    with patch.object(health.socket, "create_connection", return_value=conn) as connect:
        assert health.probe("http://user:secret@proxy.test:8080") is expected
    connect.assert_called_once_with(("proxy.test", 8080), timeout=3)
    request = conn.sendall.call_args.args[0]
    assert b"CONNECT www.youtube.com:443 HTTP/1.1\r\n" in request
    assert b"Proxy-Authorization: Basic " in request


def test_socket_timeout():
    with patch.object(health.socket, "create_connection", side_effect=TimeoutError):
        assert not health.probe("http://proxy.test:8080")


@pytest.mark.parametrize("proxy", ["socks5://proxy.test", "http://proxy.test:bad", "not-a-proxy"])
def test_invalid_proxy(proxy):
    with patch.object(health.socket, "create_connection") as connect:
        assert not health.probe(proxy)
    connect.assert_not_called()


@pytest.mark.parametrize("provider_name,settings", [
    ("webhook", {"DISCORD_WEBHOOK_URL": "https://example.test/hook"}),
    ("smtp", {"EMAIL_PROVIDER": "smtp", "EMAIL_TO": "test@example.test", "EMAIL_FROM": "from@example.test", "SMTP_HOST": "smtp.test"}),
    ("resend", {"EMAIL_PROVIDER": "resend", "EMAIL_TO": "test@example.test", "EMAIL_FROM": "from@example.test", "RESEND_API_KEY": "test-key"})])
def test_notification_reuses_outbox(tmp_path, provider_name, settings):
    from email_providers import FakeProvider
    n = health.notifications
    provider = FakeProvider()
    with patch.dict(os.environ, settings, clear=True), \
            patch.object(n, "get_configured_provider", autospec=True, return_value=provider) as factory, \
            patch.object(n, "bind_cost_root"):
        assert health.notify(tmp_path, "unhealthy", "transition") == {"stage": "send", "status": "accepted"}
    factory.assert_called_once_with(provider_name)
    assert len(provider.history) == 1
    with n.get_operations_db(tmp_path) as conn:
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        assert delivery["status"] == "accepted"
        assert provider.history[0]["idempotency_key"] == delivery["provider_idempotency_key"]
        assert conn.execute("SELECT outcome FROM delivery_attempts").fetchone()[0] == "accepted"


@pytest.mark.parametrize("outcome", ["accepted", "retryable_not_accepted", "permanent_failure", "unknown", "exception"])
def test_provider_send_outcome_is_recorded(tmp_path, capsys, outcome):
    from email_providers import FakeProvider
    provider = FakeProvider(mode=outcome if outcome != "exception" else "unknown",
                            crash_before_return=outcome == "exception", safe_error="secret")
    state = tmp_path / "health.json"
    env = {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080", "YOUTUBE_PROXY_HEALTH_STATE": str(state),
           "YOUTUBE_PROXY_HEALTH_FAILURES": "1", "DISCORD_WEBHOOK_URL": "https://example.test/hook"}
    with patch.dict(os.environ, env, clear=True), patch.object(health.pipeline, "load_dotenv"), \
            patch.object(health.notifications, "get_configured_provider", return_value=provider), \
            patch.object(health.notifications, "bind_cost_root"):
        assert run_probe(False) == 1
        expected = {"stage": "send", "status": "unknown" if outcome == "exception" else outcome}
        if outcome == "exception":
            expected["error_type"] = "OSError"
        assert json.loads(state.read_text())["notification"] == expected
        assert run_probe(False) == 1
        assert len(provider.history) == 1
    with health.notifications.get_operations_db(tmp_path) as conn:
        assert conn.execute("SELECT outcome FROM delivery_attempts").fetchone()[0] == expected["status"]
    output = capsys.readouterr()
    if outcome != "accepted":
        assert "send" in output.err
    if outcome == "exception":
        assert "OSError" in output.err
    assert "secret" not in output.out + output.err + state.read_text()
