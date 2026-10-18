"""W0 read-only web dashboard skeleton (issue #6).

Local-first, zero new dependencies: stdlib http.server + sqlite3 read-only
mode. GET search only ever touches the local FTS index — semantic/Ask paths
hit paid APIs and are deliberately NOT exposed here.

安全起點：
- 預設 loopback（127.0.0.1）；Host header 白名單只允許本機名稱
- 全部路由唯讀（DB 以 mode=ro 開啟）；拒絕寫入方法
- 不啟動 worker／排程／下載／寄信
- 錯誤對外遮罩；HTML 一律 escape
"""
from __future__ import annotations

import html
import json
import re
import socket
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class MigrationRequired(RuntimeError):
    """The dashboard never migrates an incompatible index."""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _readonly_db(root: Path) -> sqlite3.Connection:
    from pipeline import LIBRARY_SCHEMA_VERSION

    db = root / "library.db"
    if not db.is_file():
        raise FileNotFoundError(f"索引不存在：{db}")
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != LIBRARY_SCHEMA_VERSION:
            raise MigrationRequired("migration_required")
        connection.execute("SELECT job, title, source, duration, categories FROM videos LIMIT 0")
        connection.execute("SELECT job, timestamp, kind, text FROM events LIMIT 0")
    except (sqlite3.Error, MigrationRequired) as exc:
        connection.close()
        if isinstance(exc, sqlite3.Error) and exc.sqlite_errorcode == sqlite3.SQLITE_ERROR:
            raise MigrationRequired("migration_required") from exc
        raise
    return connection


def _list_videos(root: Path) -> list[dict[str, object]]:
    connection = _readonly_db(root)
    try:
        return [
            {
                "job": job,
                "title": title or job,
                "duration": duration,
                "categories": json.loads(cats or "[]"),
            }
            for job, title, duration, cats in connection.execute(
                "SELECT job, title, duration, categories FROM videos ORDER BY title"
            )
        ]
    finally:
        connection.close()


def _video_detail(root: Path, job: str) -> dict[str, object] | None:
    connection = _readonly_db(root)
    try:
        video = connection.execute(
            "SELECT job, title, source, duration, categories FROM videos WHERE job = ?", (job,)
        ).fetchone()
        if not video:
            return None
        events = [
            {"timestamp": ts, "kind": kind, "text": text}
            for ts, kind, text in connection.execute(
                "SELECT timestamp, kind, text FROM events WHERE job = ? ORDER BY timestamp", (job,)
            )
        ]
        return {
            "job": video[0], "title": video[1] or video[0], "source": video[2],
            "duration": video[3], "categories": json.loads(video[4] or "[]"), "events": events,
        }
    finally:
        connection.close()


def _search(root: Path, query: str, limit: int = 20) -> list[dict[str, object]]:
    """唯讀本機搜尋——只查 FTS 索引；語意／Ask 會觸發付費 API，不在此暴露。"""
    import pipeline

    if not query.strip():
        return []
    connection = _readonly_db(root)
    try:
        return pipeline.search_index(root, query, limit, connection=connection)
    finally:
        connection.close()


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 60rem; margin: 2rem auto; padding: 0 1rem; }}
  input[type=search] {{ width: 100%; padding: .5rem; }}
  .hit {{ border-bottom: 1px solid #ddd; padding: .5rem 0; }}
  .cats span {{ background: #eef; border-radius: 4px; padding: .1rem .4rem; margin-right: .3rem; font-size: .85rem; }}
</style></head>
<body>
<nav><a href="/">影片庫</a></nav>
<form action="/search" method="get"><input type="search" name="q" placeholder="搜尋逐字稿／摘要（僅本機索引）"></form>
{body}
</body></html>""".encode("utf-8")


def make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # quiet; operator sees requests via journal
            pass

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — stdlib hook
            # Validate the whole authority, including bracketed IPv6 and port.
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            host = hosts[0].lower() if len(hosts) == 1 else ""
            allowed = re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host) is not None
            try:
                target = urlsplit("http://" + host)
                target_port = target.port or 80
                if origins:
                    origin = urlsplit(origins[0])
                    allowed = allowed and len(origins) == 1 and (
                        origin.scheme == "http"
                        and (origin.hostname, origin.port or 80) == (target.hostname, target_port)
                        and origin.username is None and origin.password is None
                        and not (origin.path or origin.query or origin.fragment)
                    )
            except ValueError:
                allowed = False
            if not allowed:
                self._send(HTTPStatus.FORBIDDEN, _page("403", "<h1>403 Forbidden</h1>"))
                return
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, _page("400", "<h1>400 Bad Request</h1>"))
                return
            try:
                if parsed.path == "/api/health":
                    self._send(200, b'{"status":"ok"}', "application/json")
                elif parsed.path == "/":
                    videos = _list_videos(root)
                    items = "".join(
                        f'<li><a href="/video/{quote(str(v["job"]), safe="")}">{_esc(v["title"])}</a> '
                        f'<span class="cats">{"".join(f"<span>{_esc(c)}</span>" for c in v["categories"])}</span></li>'
                        for v in videos
                    )
                    self._send(200, _page("影片庫", f"<h1>影片庫（{len(videos)} 支）</h1><ul>{items}</ul>"))
                elif parsed.path == "/search":
                    query = (parse_qs(parsed.query).get("q") or [""])[0]
                    hits = _search(root, query)
                    body = "".join(
                        f'<div class="hit"><strong>{_esc(h["title"] or h["job"])}</strong> '
                        f'<em>{_esc(h["kind"])}</em><br>{_esc(h["snippet"])}</div>'
                        for h in hits
                    )
                    self._send(200, _page(f"搜尋：{query}", f"<h1>搜尋「{_esc(query)}」— {len(hits)} 筆</h1>{body}"))
                elif parsed.path.startswith("/video/"):
                    job = unquote(parsed.path.removeprefix("/video/"))
                    detail = _video_detail(root, job)
                    if not detail:
                        self._send(HTTPStatus.NOT_FOUND, _page("404", "<h1>404 Not Found</h1>"))
                        return
                    events = "".join(
                        f"<li><code>{_esc(e['timestamp'])}</code> [{_esc(e['kind'])}] {_esc(e['text'][:200])}</li>"
                        for e in detail["events"]
                    )
                    self._send(200, _page(str(detail["title"]),
                        f'<h1>{_esc(detail["title"])}</h1>'
                        f'<p class="cats">{"".join(f"<span>{_esc(c)}</span>" for c in detail["categories"])}</p>'
                        f"<ul>{events}</ul>"))
                else:
                    self._send(HTTPStatus.NOT_FOUND, _page("404", "<h1>404 Not Found</h1>"))
            except MigrationRequired:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, _page(
                    "migration_required", "<h1>migration_required</h1><p>請先停止 dashboard，再由明確的索引維護流程更新資料庫。</p>"
                ))
            except FileNotFoundError:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, _page(
                    "index_required", "<h1>index_required</h1><p>請先以 --index 建立本機索引。</p>"
                ))
            except Exception:
                # 錯誤遮罩：不把例外內容洩給瀏覽器
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, _page("500", "<h1>500 Internal Server Error</h1>"))

        def do_POST(self) -> None:  # noqa: N802
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"405 Method Not Allowed", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST  # type: ignore[assignment]
        do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = do_POST

    return Handler


class _IPv6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(root: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    host = host.lower()
    if host not in LOCAL_HOSTS:
        raise ValueError("W0 dashboard 僅允許 loopback；遠端存取須先具備認證與安全通道。")
    if not 0 <= port <= 65535:
        raise ValueError("port 必須介於 0 與 65535。")
    host = "::1" if host == "[::1]" else "127.0.0.1" if host == "localhost" else host
    server_type = _IPv6HTTPServer if host == "::1" else ThreadingHTTPServer
    server = server_type((host, port), make_handler(root))
    try:
        address = f"[{host}]" if host == "::1" else host
        print(f"唯讀 dashboard：http://{address}:{server.server_address[1]}/（Ctrl+C 結束）")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
