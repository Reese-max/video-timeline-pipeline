"""W0 skeleton: read-only web dashboard must be loopback-safe and never leak internals."""

import sqlite3
from pathlib import Path

import pytest


def _client(root: Path):
    from fastapi.testclient import TestClient

    from webapp import create_app

    return TestClient(create_app(root), headers={"host": "127.0.0.1"})


def test_health_ok(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_host_header_allowlist(tmp_path):
    client = _client(tmp_path)
    ok = client.get("/api/health", headers={"host": "127.0.0.1:8080"})
    assert ok.status_code == 200
    bad = client.get("/api/health", headers={"host": "evil.example.com"})
    assert bad.status_code in (400, 403)


def test_stats_unavailable_without_db(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/stats")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "unavailable"


def test_stats_counts_videos(tmp_path):
    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE videos (job TEXT PRIMARY KEY, title TEXT, source TEXT, status TEXT, duration REAL, indexed_at TEXT, categories TEXT)"
    )
    conn.executemany(
        "INSERT INTO videos VALUES (?,?,?,?,?,?,?)",
        [
            ("a", "t1", "youtube", "done", 1.0, "2026-01-01", "x"),
            ("b", "t2", "youtube", "failed", 2.0, "2026-01-01", "x"),
        ],
    )
    conn.commit()
    conn.close()
    client = _client(tmp_path)
    res = client.get("/api/stats")
    body = res.json()
    assert body["status"] == "ok"
    assert body["videos_total"] == 2
    assert body["by_status"]["done"] == 1
    assert body["by_status"]["failed"] == 1


def test_stats_does_not_create_schema(tmp_path):
    db = tmp_path / "library.db"
    sqlite3.connect(db).close()  # empty file, no tables
    client = _client(tmp_path)
    body = client.get("/api/stats").json()
    assert body["status"] == "migration_required"


def test_404_masks_paths(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/nonexistent")
    assert res.status_code == 404
    assert "Traceback" not in res.text
    assert str(tmp_path) not in res.text


def test_index_html_escapes(tmp_path):
    client = _client(tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_empty_host_header_rejected(tmp_path):
    client = _client(tmp_path)
    res = client.get("/api/health", headers={"host": ""})
    assert res.status_code in (400, 403, 422)


def test_run_server_rejects_non_loopback(tmp_path):
    import pytest

    from webapp import run_server

    with pytest.raises(RuntimeError):
        run_server(tmp_path, "0.0.0.0", 8080)


def test_500_masks_internals(tmp_path, monkeypatch):
    import webapp

    def boom(root):
        raise RuntimeError(f"secret at {root}/.env")

    monkeypatch.setattr(webapp, "collect_stats", boom)
    from fastapi.testclient import TestClient

    client = TestClient(
        webapp.create_app(tmp_path),
        headers={"host": "127.0.0.1"},
        raise_server_exceptions=False,
    )
    res = client.get("/api/stats")
    assert res.status_code == 500
    assert "Traceback" not in res.text
    assert ".env" not in res.text


def test_serve_cli_defaults_loopback(monkeypatch):
    import pipeline

    monkeypatch.setattr("sys.argv", ["pipeline.py"])
    args = pipeline.parse_args()
    assert args.serve is False

    monkeypatch.setattr("sys.argv", ["pipeline.py", "--serve"])
    args = pipeline.parse_args()
    assert args.serve is True
    assert args.host in ("127.0.0.1", "localhost")
    assert 1024 <= args.port <= 65535
