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
            patch.object(health.pipeline, "load_dotenv"), patch.object(health, "notify") as notify:
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
    assert json.loads(state.read_text()) == {"status": "healthy", "failures": 0}


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


def test_notification_failure_keeps_health_exit(configured):
    _, notify = configured
    notify.side_effect = RuntimeError("secret")
    with patch.dict(os.environ, {"YOUTUBE_PROXY_HEALTH_FAILURES": "1"}):
        assert run_probe(False) == 1
    assert run_probe(True) == 0


def test_absent_notification_config_is_safe(tmp_path, capsys):
    with patch.dict(os.environ, {}, clear=True), patch.object(health.notifications, "get_operations_db") as db:
        health.notify(tmp_path, "unhealthy", "transition")
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


def test_notification_reuses_outbox(tmp_path):
    n = health.notifications
    provider = MagicMock()
    provider.send.return_value = n.DeliveryResult(status="accepted")
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.test/hook"}, clear=True), \
            patch.object(n, "get_configured_provider", return_value=provider), patch.object(n, "bind_cost_root"):
        health.notify(tmp_path, "unhealthy", "transition")
    provider.send.assert_called_once()
    with n.get_operations_db(tmp_path) as conn:
        assert conn.execute("SELECT status FROM notification_deliveries").fetchone()[0] == "accepted"
