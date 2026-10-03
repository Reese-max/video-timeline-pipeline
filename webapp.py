"""Read-only web dashboard skeleton (issue #6 W0).

Optional dependency: FastAPI + uvicorn must be installed; the core CLI
keeps working without them. Everything here is read-only against existing
artifacts — no workers, downloads, embedding, or sending side effects.
"""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:  # pragma: no cover - optional dependency
    FastAPI = None  # type: ignore[assignment]

    class Request:  # type: ignore[no-redef]
        pass

    class HTMLResponse:  # type: ignore[no-redef]
        pass

    class JSONResponse:  # type: ignore[no-redef]
        pass

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _normalize_host(value: str) -> str:
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _host_allowed(host_header: str, allowed: tuple[str, ...]) -> bool:
    name = _normalize_host(host_header.split("@")[-1])
    if not name:
        return False
    allowed_names = {_normalize_host(entry) for entry in allowed}
    return name in allowed_names


def _read_only_connect(path: Path) -> sqlite3.Connection:
    ingest_uri = path.absolute().as_uri()
    return sqlite3.connect(ingest_uri + "?mode=ro", uri=True)


def collect_stats(root: Path) -> dict[str, Any]:
    db = Path(root) / "library.db"
    if not db.is_file():
        return {"status": "unavailable", "reason": "library_db_missing"}
    try:
        conn = _read_only_connect(db)
    except sqlite3.Error:
        return {"status": "unavailable", "reason": "library_db_unreadable"}
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "videos" not in tables:
            return {"status": "migration_required", "reason": "videos_table_missing"}
        total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        by_status: dict[str, int] = {}
        for status, count in conn.execute(
            "SELECT COALESCE(status, 'unknown'), COUNT(*) FROM videos GROUP BY 1"
        ):
            by_status[str(status)] = int(count)
        return {"status": "ok", "videos_total": int(total), "by_status": by_status}
    except sqlite3.Error as exc:
        return {"status": "error", "reason": type(exc).__name__}
    finally:
        conn.close()


def create_app(root: Path | str, allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS):
    if FastAPI is None:
        raise RuntimeError("FastAPI 未安裝；pip install fastapi uvicorn 後再使用 --serve")
    root = Path(root)
    app = FastAPI(title="video-timeline-pipeline", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def host_allowlist(request: Request, call_next):
        host = request.headers.get("host", "")
        if not _host_allowed(host, allowed_hosts):
            return JSONResponse({"error": {"code": "forbidden_host"}}, status_code=403)
        return await call_next(request)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

    @app.exception_handler(500)
    async def server_error(request: Request, exc):
        return JSONResponse({"error": {"code": "internal_error"}}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/stats")
    def stats():
        return collect_stats(root)

    @app.get("/", response_class=HTMLResponse)
    def index():
        stats_payload = collect_stats(root)
        rows = "".join(
            f"<li>{html.escape(str(k))}: {html.escape(str(v))}</li>"
            for k, v in sorted(stats_payload.items())
        )
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>video-timeline-pipeline</title></head><body>"
            "<h1>video-timeline-pipeline</h1><ul>"
            f"{rows}</ul></body></html>"
        )
        return HTMLResponse(page)

    return app


def run_server(root: Path | str, host: str, port: int) -> int:
    try:
        import uvicorn
    except ImportError:
        raise RuntimeError("uvicorn 未安裝；pip install uvicorn 後再使用 --serve")
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(f"--serve 預設僅 loopback；拒絕綁定 {host!r}")
    app = create_app(root)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
