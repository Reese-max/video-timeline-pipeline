"""Local-first web dashboard for the video timeline pipeline (issue #6).

W0/W8 security baseline, stdlib-only — no FastAPI/Jinja2 dependency, so
the existing CLI works identically whether or not any web stack is
installed. The server is read-mostly:

- GET endpoints read local artifacts only — they never call embeddings,
  LLMs, remote providers, or download models.
- Side-effecting POSTs (semantic search, Ask, RSS retry, digest send) go
  through the durable ``operation_tasks`` queue and return 202 + task id.
- Loopback by default with a Host allowlist, Origin/CSRF enforcement on
  POSTs, masked errors, and autoescaped HTML.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import os
import secrets
import signal
import socket
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import web_services
import web_tasks

log = logging.getLogger("webapp")

SCHEMA_VERSION = "1"
MAX_BODY_BYTES = 64 * 1024
POST_RATE_LIMIT = 60  # POSTs per minute per client IP
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}

_ERROR_STATUS = {
    "invalid_param": 400,
    "invalid_host": 400,
    "not_found": 404,
    "method_not_allowed": 405,
    "csrf_failed": 403,
    "origin_not_allowed": 403,
    "unauthorized": 401,
    "budget_blocked": 403,
    "not_retryable": 409,
    "unknown_state": 409,
    "stale_preview": 409,
    "already_sent": 409,
    "payload_too_large": 413,
    "rate_limited": 429,
    "index_unavailable": 503,
    "migration_required": 503,
    "operations_unavailable": 503,
    "internal_error": 500,
    "search_failed": 400,
}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.root: Path = Path(".")
        self.csrf_token: str = ""
        self.auth_token: str | None = None
        self.remote_mode: bool = False
        self.allowed_hosts: set[str] = set()
        self._post_log: dict[str, list[float]] = {}
        self._post_log_lock = threading.Lock()
        super().__init__(*args, **kwargs)


def _split_host(value: str) -> str:
    """Host header -> bare hostname (handles [::1]:8080 and host:port)."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1 : value.index("]")] if "]" in value else value.strip("[]")
    return value.split(":")[0] if value.count(":") == 1 else value


def _is_loopback(host: str) -> bool:
    name = _split_host(host)
    if name in {"localhost", "127.0.0.1", "::1", "ip6-localhost"}:
        return True
    try:
        return socket.getaddrinfo(name, None)[0][4][0].startswith("127.")
    except (socket.gaierror, OSError):
        return False


def build_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    auth_token: str | None = None,
    allow_remote: bool = False,
    trusted_proxy: bool = False,
    remote_mode: bool = False,
    extra_allowed_hosts: list[str] | None = None,
) -> DashboardServer:
    """Build the HTTP server. Non-loopback binds require all three of:
    explicit allow_remote, a configured auth token, and an asserted
    trusted TLS proxy / private channel — otherwise refuse to start (W8).
    """
    root = Path(root).resolve()
    loopback = _is_loopback(host)
    if not loopback:
        if not allow_remote:
            raise ValueError("refusing non-loopback bind without --serve-allow-remote")
        if not auth_token:
            raise ValueError("refusing non-loopback bind without an auth token (WEB_AUTH_TOKEN/--serve-auth-token)")
        if not trusted_proxy:
            raise ValueError("refusing non-loopback bind without a trusted TLS proxy/tunnel (--serve-trusted-proxy)")
        remote_mode = True

    server = DashboardServer((host, port), DashboardHandler)
    server.root = root
    server.csrf_token = secrets.token_urlsafe(32)
    server.auth_token = auth_token or None
    server.remote_mode = remote_mode
    server.allowed_hosts = {
        "localhost",
        "127.0.0.1",
        "::1",
        _split_host(host),
        *(_split_host(h) for h in (extra_allowed_hosts or []) if h.strip()),
    }
    return server


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    auth_token: str | None = None,
    allow_remote: bool = False,
    trusted_proxy: bool = False,
) -> int:
    """Run the dashboard until SIGINT/SIGTERM; graceful shutdown (W0)."""
    server = build_server(
        root,
        host=host,
        port=port,
        auth_token=auth_token,
        allow_remote=allow_remote,
        trusted_proxy=trusted_proxy,
    )
    bound_host, bound_port = server.server_address[:2]

    def _stop(_sig: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(f"Dashboard: http://{_display_host(bound_host)}:{bound_port}/ （唯讀介面；POST 會進持久化工作佇列）")
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


def _display_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"
    timeout = 30

    # --- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # keep access log secret-free
        log.info("%s %s", self.address_string(), fmt % args)

    def _request_id(self) -> str:
        rid = getattr(self, "_rid", None)
        if not rid:
            rid = uuid.uuid4().hex[:16]
            self._rid = rid
        return rid

    def _security_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "X-Request-Id": self._request_id(),
        }
        if self.close_connection:
            headers["Connection"] = "close"
        if extra:
            headers.update(extra)
        return headers

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in self._security_headers({"Content-Type": "application/json; charset=utf-8"}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, status: int, markup: str) -> None:
        body = markup.encode("utf-8")
        self.send_response(status)
        for key, value in self._security_headers({"Content-Type": "text/html; charset=utf-8"}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, code: str, safe_message: str, *, status: int | None = None,
               retryable: bool = False) -> None:
        self._send_json(
            status or _ERROR_STATUS.get(code, 500),
            {
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": code,
                    "safe_message": safe_message,
                    "retryable": retryable,
                    "request_id": self._request_id(),
                },
            },
        )

    # --- request gates ----------------------------------------------------

    def _check_host(self) -> bool:
        host = _split_host(self.headers.get("Host") or "")
        if not host:
            self._error("invalid_host", "missing Host header")
            return False
        if host not in self.server.allowed_hosts:
            self._error("invalid_host", "host not allowed")
            return False
        return True

    def _bearer_ok(self) -> bool:
        expected = self.server.auth_token
        if not expected:
            return False
        presented = self.headers.get("Authorization", "")
        return secrets.compare_digest(presented, f"Bearer {expected}")

    def _check_read_auth(self) -> bool:
        """Remote mode requires bearer auth on every request (W8)."""
        if self.server.remote_mode and not self._bearer_ok():
            self._error("unauthorized", "bearer token required", status=401)
            return False
        return True

    def _check_write_auth(self) -> bool:
        """POSTs need a valid local session (CSRF token) or explicit bearer."""
        if self._bearer_ok():
            origin = self.headers.get("Origin")
            if origin and not self._origin_allowed(origin):
                self._error("origin_not_allowed", "origin not allowed")
                return False
            return True
        origin = self.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            self._error("origin_not_allowed", "origin not allowed")
            return False
        token = self.headers.get("X-CSRF-Token", "")
        if not token or not secrets.compare_digest(token, self.server.csrf_token):
            self._error("csrf_failed", "valid X-CSRF-Token required")
            return False
        return True

    def _origin_allowed(self, origin: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        return host in self.server.allowed_hosts

    def _check_rate_limit(self) -> bool:
        now = time.monotonic()
        key = self.client_address[0]
        with self.server._post_log_lock:
            hits = [t for t in self.server._post_log.get(key, []) if now - t < 60.0]
            if len(hits) >= POST_RATE_LIMIT:
                self._error("rate_limited", "too many requests", retryable=True)
                return False
            hits.append(now)
            self.server._post_log[key] = hits
        return True

    def _read_body(self) -> dict[str, Any] | None:
        length = self.headers.get("Content-Length")
        try:
            size = int(length or 0)
        except ValueError:
            self._error("invalid_param", "bad Content-Length")
            return None
        if size > MAX_BODY_BYTES:
            # Reject and drop the connection: an unread oversized body would
            # otherwise be parsed as the next request line on keep-alive.
            self.close_connection = True
            self._error("payload_too_large", "body too large")
            return None
        raw = self.rfile.read(size) if size else b"{}"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if ctype == "application/x-www-form-urlencoded":
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
            data = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error("invalid_param", "malformed JSON body")
            return None
        if not isinstance(data, dict):
            self._error("invalid_param", "JSON object body expected")
            return None
        return data

    # --- dispatch ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._check_host():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if method == "GET":
                if not self._check_read_auth():
                    return
                self._route_get(path, query)
            elif method == "POST":
                if not self._check_read_auth():
                    return
                if not self._check_rate_limit():
                    return
                if not self._check_write_auth():
                    return
                self._route_post(path)
            else:
                # Unread bodies corrupt keep-alive parsing — drop the conn.
                if _int(self.headers.get("Content-Length"), 0) > 0:
                    self.close_connection = True
                self._error("method_not_allowed", "method not allowed", status=405)
        except web_services.ServiceError as exc:
            self._error(exc.code, exc.safe_message, status=exc.http_status, retryable=exc.retryable)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("unhandled error in %s %s", method, self.path)
            self._error("internal_error", "internal error", retryable=True)

    # --- GET routes --------------------------------------------------------

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        first = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        if path == "/api/health":
            self._send_json(200, {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "scope": "local-dashboard",
                "csrf_token": None if self.server.remote_mode else self.server.csrf_token,
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                "index": _index_state(self.server.root),
                "operations_db": web_services.operations_db_available(self.server.root),
            })
            return
        if path == "/api/stats":
            self._send_json(200, web_services.dashboard_stats(self.server.root))
            return
        if path == "/api/videos":
            self._send_json(200, web_services.list_videos_page(
                self.server.root,
                page=_int(first("page", "1"), 1),
                page_size=_int(first("page_size", str(web_services.DEFAULT_PAGE_SIZE)), web_services.DEFAULT_PAGE_SIZE),
                sort=first("sort", "indexed_at") or "indexed_at",
                order=(first("order", "desc") or "desc").lower(),
                filters={
                    "job": first("job"), "status": first("status"), "category": first("category"),
                    "author": first("author"), "platform": first("platform"),
                    "date_field": first("date_field"),
                    "date_from": first("date_from"), "date_to": first("date_to"),
                },
            ))
            return
        if path.startswith("/api/videos/"):
            job = urllib.parse.unquote(path[len("/api/videos/"):])
            if first("part") == "transcript":
                self._send_json(200, web_services.get_transcript_window(
                    self.server.root, job,
                    offset=_int(first("offset", "0"), 0), limit=_int(first("limit", "50000"), 50000),
                ))
                return
            self._send_json(200, web_services.get_video_detail(
                self.server.root, job,
                timeline_offset=_int(first("timeline_offset", "0"), 0),
                timeline_limit=_int(first("timeline_limit", "200"), 200),
            ))
            return
        if path == "/api/search":
            self._send_json(200, web_services.search_local(
                self.server.root, first("q"),
                limit=_int(first("limit", "20"), 20), category=first("category"),
            ))
            return
        if path == "/api/operations":
            self._send_json(200, web_services.rss_overview(self.server.root))
            return
        if path == "/api/operations/failed" or path == "/api/jobs/failed":
            self._send_json(200, web_services.failed_operations(self.server.root))
            return
        if path == "/api/operations/providers":
            self._send_json(200, web_services.provider_health(self.server.root))
            return
        if path == "/api/costs":
            self._send_json(200, web_services.cost_report(self.server.root, first("period", "daily") or "daily"))
            return
        if path == "/api/budget-status":
            self._send_json(200, web_services.budget_status(self.server.root))
            return
        if path == "/api/digests":
            self._send_json(200, web_services.list_digests(self.server.root, _int(first("limit", "50"), 50)))
            return
        if path == "/api/notifications":
            self._send_json(200, web_services.list_notifications(self.server.root, _int(first("limit", "50"), 50)))
            return
        if path == "/api/tasks":
            self._send_json(200, {
                "schema_version": SCHEMA_VERSION,
                "items": web_tasks.list_tasks(self.server.root, status=first("status") or None,
                                              limit=_int(first("limit", "50"), 50)),
            })
            return
        if path.startswith("/api/tasks/"):
            task_id = urllib.parse.unquote(path[len("/api/tasks/"):])
            task = web_tasks.get_task(self.server.root, task_id)
            if task is None:
                raise web_services.ServiceError("not_found", "unknown task", http_status=404)
            self._send_json(200, {"schema_version": SCHEMA_VERSION, **task})
            return
        # HTML pages
        if path == "/":
            self._send_html(200, _page_home(self.server))
            return
        if path == "/videos":
            self._send_html(200, _page_videos(self.server, query))
            return
        if path.startswith("/videos/"):
            self._send_html(200, _page_video_detail(self.server, urllib.parse.unquote(path[len("/videos/"):])) )
            return
        if path == "/search":
            self._send_html(200, _page_search(self.server, first("q")))
            return
        if path == "/operations":
            self._send_html(200, _page_operations(self.server))
            return
        if path == "/operations/failed":
            self._send_html(200, _page_operations_failed(self.server))
            return
        if path == "/operations/providers":
            self._send_html(200, _page_providers(self.server))
            return
        if path == "/digests":
            self._send_html(200, _page_digests(self.server))
            return
        if path == "/notifications":
            self._send_html(200, _page_notifications(self.server))
            return
        if path == "/tasks":
            self._send_html(200, _page_tasks(self.server))
            return
        self._error("not_found", "not found", status=404)

    # --- POST routes -------------------------------------------------------

    def _route_post(self, path: str) -> None:
        body = self._read_body()
        if body is None:
            return
        idem = (self.headers.get("Idempotency-Key") or "").strip()[:128] or None
        if path == "/api/search":
            mode = str(body.get("mode") or "semantic")
            if mode not in ("semantic",):
                raise web_services.ServiceError("invalid_param", "mode must be 'semantic' (paid search)", http_status=400)
            self._send_json(202, web_services.enqueue_semantic_search(
                self.server.root, str(body.get("query") or ""),
                limit=_int(body.get("limit"), 20), idempotency_key=idem,
            ))
            return
        if path == "/api/ask":
            self._send_json(202, web_services.enqueue_ask(
                self.server.root, str(body.get("question") or body.get("query") or ""),
                limit=_int(body.get("limit"), 12), idempotency_key=idem,
            ))
            return
        if path == "/api/digest/preview":
            self._send_json(200, web_services.preview_digest(
                self.server.root,
                digest_type=str(body.get("digest_type") or "daily"),
                date_str=str(body.get("date")) if body.get("date") else None,
            ))
            return
        if path == "/api/digest/send":
            self._send_json(202, web_services.confirm_digest_send(
                self.server.root,
                digest_id=str(body.get("digest_id") or ""),
                revision=_int(body.get("revision"), 0),
                payload_hash=str(body.get("payload_hash") or ""),
                idempotency_key=idem,
            ))
            return
        if path.startswith("/api/jobs/") and path.endswith("/retry"):
            item_id = urllib.parse.unquote(path[len("/api/jobs/"):-len("/retry")])
            self._send_json(202, web_services.enqueue_rss_retry(
                self.server.root, item_id, idempotency_key=idem,
            ))
            return
        if path.startswith("/api/tasks/") and path.endswith("/cancel"):
            task_id = urllib.parse.unquote(path[len("/api/tasks/"):-len("/cancel")])
            try:
                status = web_tasks.request_cancel(self.server.root, task_id)
            except KeyError:
                raise web_services.ServiceError("not_found", "unknown task", http_status=404)
            except ValueError as exc:
                raise web_services.ServiceError("not_retryable", str(exc), http_status=409)
            self._send_json(200, {"schema_version": SCHEMA_VERSION, "task_id": task_id, "status": status})
            return
        self._error("not_found", "not found", status=404)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _index_state(root: Path) -> str:
    try:
        web_services.connect_library_ro(root).close()
        return "ready"
    except web_services.ServiceError as exc:
        return exc.code


# ---------------------------------------------------------------------------
# HTML pages — every interpolated value is html.escape'd (autoescape, W8)
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#0f1115;color:#e8eaed}
nav{background:#1c1f26;padding:.7rem 1.2rem;display:flex;gap:1rem;flex-wrap:wrap}
nav a{color:#9ecbff;text-decoration:none}
main{padding:1.2rem;max-width:1100px;margin:0 auto}
table{border-collapse:collapse;width:100%;margin:1rem 0}
th,td{padding:.35rem .6rem;border-bottom:1px solid #2c313a;text-align:left;font-size:.9rem}
.card{background:#1c1f26;border:1px solid #2c313a;border-radius:8px;padding:1rem;margin:.8rem 0}
.muted{color:#9aa0a6}.err{color:#ff8a80}.ok{color:#80d8a7}
input,select,button{background:#262a33;color:#e8eaed;border:1px solid #3a3f4a;border-radius:6px;padding:.4rem .6rem}
a{color:#9ecbff}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _safe_url(value: Any) -> str:
    """Only http(s) URLs may become hrefs — model/metadata text is not trusted."""
    url = str(value or "")
    scheme = urllib.parse.urlparse(url).scheme.lower()
    return url if scheme in ("http", "https") else ""


def _page(server: DashboardServer, title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{_e(server.csrf_token)}">
<title>{_e(title)} — 影片情報 Dashboard</title><style>{_CSS}</style></head><body>
<nav><a href="/">總覽</a><a href="/videos">影片庫</a><a href="/search">搜尋</a>
<a href="/operations">作業</a><a href="/operations/failed">失敗</a><a href="/operations/providers">供應商</a>
<a href="/digests">摘要</a><a href="/notifications">通知</a><a href="/tasks">工作</a></nav>
<main>{body}</main></body></html>"""


def _state_block(available: bool, label_when_missing: str) -> str:
    return "" if available else f'<p class="muted">{_e(label_when_missing)}</p>'


def _page_home(server: DashboardServer) -> str:
    stats = web_services.dashboard_stats(server.root)
    cost = stats["cost"]
    scoring = stats["scoring"]
    cost_html = (
        f'<p>本日估計支出：<strong>{_e(cost.get("grand_total_estimated_usd"))}</strong> USD'
        f'（{_e(cost.get("period"))}）<br><span class="muted">{_e(cost.get("note"))}</span></p>'
        if cost.get("available")
        else '<p class="muted">成本資料：unavailable（尚無 operations.db）</p>'
    )
    scoring_html = (
        f'<p>高重要性影片：<strong>{_e(scoring["high_importance"])}</strong>'
        f'<span class="muted">（{_e(scoring["label"])}）</span></p>'
        if scoring.get("available")
        else '<p class="muted">尚未評分</p>'
    )
    body = f"""
<div class="card"><h2>新影片</h2>
<p>今日 <strong>{stats["new_videos"]["today"]}</strong>・本週 <strong>{stats["new_videos"]["this_week"]}</strong>
<span class="muted">（依 {_e(stats["new_videos"]["field"])}，重跑索引不算新片）</span></p>
<p class="muted">視窗：{_e(stats["window"]["today_start"])} 起・時區 {_e(stats["timezone"])}・as_of {_e(stats["as_of"])}・scope {_e(stats["scope"])}</p></div>
<div class="card"><h2>佇列狀態</h2>
<p>待處理 {stats["rss"]["queued"]}・processing {stats["rss"]["processing"]}・
<a href="/operations/failed">failed {stats["rss"]["failed"]}</a>・
unknown {stats["rss"]["unknown"]}・dead-letter {stats["rss"]["dead_letter"]}
・預算阻擋 {_e(stats["blocked_operations"] if stats["blocked_operations"] is not None else "n/a")}</p></div>
<div class="card"><h2>成本</h2>{cost_html}</div>
<div class="card"><h2>評分</h2>{scoring_html}</div>
<div class="card"><h2>索引</h2><p>library.db：{"可用" if stats["videos"]["indexed"] else "unavailable／migration_required"}</p></div>
"""
    return _page(server, "總覽", body)


def _page_videos(server: DashboardServer, query: dict[str, list[str]]) -> str:
    first = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
    try:
        data = web_services.list_videos_page(
            server.root,
            page=_int(first("page", "1"), 1),
            page_size=_int(first("page_size", "50"), 50),
            sort=first("sort", "indexed_at") or "indexed_at",
            order=(first("order", "desc") or "desc").lower(),
            filters={
                "job": first("job"), "status": first("status"), "category": first("category"),
                "author": first("author"), "platform": first("platform"),
                "date_field": first("date_field"),
                "date_from": first("date_from"), "date_to": first("date_to"),
            },
        )
    except web_services.ServiceError as exc:
        return _page(server, "影片庫", f'<div class="card"><p class="err">{_e(exc.code)}：{_e(exc.safe_message)}</p></div>')
    rows = "".join(
        "<tr>"
        f'<td><a href="/videos/{_e(v["job"])}">{_e(v["title"] or v["job"])}</a></td>'
        f"<td>{_e(v['platform'])}</td><td>{_e(v['author'] or '未知')}</td>"
        f"<td>{_e(v['published_at'] or '未知')}</td><td>{_e(v['first_completed_at'] or '未知')}</td>"
        f"<td>{_e(round(v['duration_seconds'] or 0, 1))}s</td>"
        f"<td>{_e(', '.join(v['categories']))}</td><td>{_e(v['status'])}</td>"
        f"<td>{_e(v['score']['priority_score'] if v['score'] else '未知')}</td>"
        "</tr>"
        for v in data["items"]
    )
    empty = "" if data["items"] else '<p class="muted">沒有符合條件的影片。</p>'
    page = data["page"]
    total_pages = max(1, (data["total"] + data["page_size"] - 1) // data["page_size"])
    pager = (
        f'<p class="muted">第 {page}/{total_pages} 頁・共 {data["total"]} 筆</p>'
        f'<a href="/videos?page={page - 1}">上一頁</a> ' if page > 1 else ""
    ) + (f'<a href="/videos?page={page + 1}">下一頁</a>' if page < total_pages else "")
    body = f"""
<div class="card"><form method="get" action="/videos">
<input name="category" placeholder="分類（精確）" value="{_e(first('category'))}">
<input name="author" placeholder="作者" value="{_e(first('author'))}">
<input name="status" placeholder="狀態" value="{_e(first('status'))}">
<button>篩選</button></form></div>
<div class="card"><table><tr><th>標題</th><th>平台</th><th>作者</th><th>發佈</th><th>完成</th><th>長度</th><th>分類</th><th>狀態</th><th>分數</th></tr>
{rows}</table>{empty}{pager}</div>"""
    return _page(server, "影片庫", body)


def _page_video_detail(server: DashboardServer, job: str) -> str:
    try:
        d = web_services.get_video_detail(server.root, job)
    except web_services.ServiceError as exc:
        return _page(server, "影片", f'<div class="card"><p class="err">{_e(exc.code)}：{_e(exc.safe_message)}</p></div>')
    entries = "".join(
        f"<tr><td>{_e(e.get('start'))}–{_e(e.get('end'))}s</td><td>{_e(e.get('label'))}</td><td>{_e(e.get('detail'))}</td></tr>"
        for e in d["timeline"]["entries"]
    )
    body = f"""
<div class="card"><h2>{_e(d['title'])}</h2>
<p class="muted">job {_e(d['job'])}・{_e(d['platform'])}・狀態 {_e(d['status'])}・完成 {_e(d['first_completed_at'])}（{_e(d['first_completed_field'])}）</p>
<p>摘要（{_e(d['summary']['time_scope'])}）：{_e(d['summary']['brief'])}</p>
<p>來源：{f'<a href="' + _e(_safe_url(d["source"].get("url"))) + '" rel="noopener noreferrer">' + _e(d["source"].get("url") or "未知") + "</a>" if _safe_url(d["source"].get("url")) else _e(d["source"].get("url") or "未知")}</p></div>
<div class="card"><h3>時間軸（{d['timeline']['total']} 段）</h3>
<table><tr><th>區間</th><th>段落</th><th>說明</th></tr>{entries}</table></div>"""
    return _page(server, d["title"], body)


def _page_search(server: DashboardServer, q: str) -> str:
    results_html = ""
    if q.strip():
        try:
            data = web_services.search_local(server.root, q, 20)
            rows = "".join(
                f'<tr><td><a href="/videos/{_e(h["job"])}">{_e(h["title"] or h["job"])}</a></td>'
                f"<td>{_e(h['kind'])}</td><td>{_e(h['timestamp'])}</td><td>{_e(h['snippet'])}</td></tr>"
                for h in data["results"]
            )
            if not data["results"]:
                results_html = '<div class="card"><p class="muted">無結果。</p></div>'
            else:
                results_html = (
                    '<div class="card"><table><tr><th>影片</th><th>種類</th><th>時間</th><th>摘錄</th></tr>'
                    f"{rows}</table></div>"
                )
        except web_services.ServiceError as exc:
            results_html = f'<div class="card"><p class="err">{_e(exc.code)}：{_e(exc.safe_message)}</p></div>'
    body = f"""
<div class="card"><form method="get" action="/search">
<input name="q" value="{_e(q)}" maxlength="200" size="60" placeholder="本機全文搜尋">
<button>搜尋</button>
<p class="muted">GET 搜尋只讀本機索引（external_calls=0）。語意檢索／Ask 會消耗額度，請用 POST /api/search、/api/ask（需 CSRF／授權，進 durable task）。</p>
</form></div>{results_html}"""
    return _page(server, "搜尋", body)


def _page_operations(server: DashboardServer) -> str:
    data = web_services.rss_overview(server.root)
    items = "".join(
        f"<tr><td>{_e(i['id'])}</td><td>{_e(i['title'])}</td><td>{_e(i['status'])}</td>"
        f"<td>{_e(i['attempts'])}</td><td>{_e(i['safe_error'])}</td></tr>"
        for i in data["items"]
    )
    subs = "".join(
        f"<tr><td>{_e(s['id'])}</td><td>{_e(s['kind'])}</td><td>{_e(s['name'])}</td>"
        f"<td>{'啟用' if s['enabled'] else '暫停'}</td><td>{_e(s['safe_error'])}</td></tr>"
        for s in data["subscriptions"]
    )
    body = f"""
<div class="card"><p class="muted">來源：{_e(data['source'])}・updated_at {_e(data['updated_at'] or 'n/a')}</p>
<h3>訂閱</h3><table><tr><th>ID</th><th>種類</th><th>名稱</th><th>狀態</th><th>最近錯誤</th></tr>{subs}</table>
<h3>佇列項目</h3><table><tr><th>ID</th><th>標題</th><th>狀態</th><th>嘗試</th><th>錯誤（已遮罩）</th></tr>{items}</table></div>"""
    return _page(server, "作業", body)


def _page_operations_failed(server: DashboardServer) -> str:
    data = web_services.failed_operations(server.root)
    rss_rows = "".join(
        f"<tr><td>{_e(i['id'])}</td><td>{_e(i['title'])}</td><td>{_e(i['status'])}</td><td>{_e(i['safe_error'])}</td></tr>"
        for i in data["rss_failed"]
    )
    cost_rows = "".join(
        f"<tr><td>{_e(i.get('operation'))}</td><td>{_e(i.get('provider'))}</td><td>{_e(i.get('job'))}</td><td>{_e(i.get('safe_error'))}</td></tr>"
        for i in data["cost_failed"]
    )
    task_rows = "".join(
        f"<tr><td>{_e(i.get('task_id'))}</td><td>{_e(i.get('type'))}</td><td>{_e(i.get('status'))}</td><td>{_e(i.get('safe_error'))}</td></tr>"
        for i in data["tasks_failed"]
    )
    body = f"""
<div class="card"><h3>RSS 失敗／unknown</h3><table><tr><th>ID</th><th>標題</th><th>狀態</th><th>錯誤</th></tr>{rss_rows or '<tr><td colspan="4" class="muted">無</td></tr>'}</table></div>
<div class="card"><h3>成本記錄中的失敗／阻擋</h3><table><tr><th>操作</th><th>供應商</th><th>job</th><th>錯誤</th></tr>{cost_rows or '<tr><td colspan="4" class="muted">無</td></tr>'}</table></div>
<div class="card"><h3>失敗的工作</h3><table><tr><th>task</th><th>類型</th><th>狀態</th><th>錯誤</th></tr>{task_rows or '<tr><td colspan="4" class="muted">無</td></tr>'}</table></div>"""
    return _page(server, "失敗監控", body)


def _page_providers(server: DashboardServer) -> str:
    data = web_services.provider_health(server.root)
    rows = "".join(
        f"<tr><td>{_e(p['provider'])}</td><td>{_e(p['total_calls'])}</td><td>{_e(p['success_rate_pct'])}%</td>"
        f"<td>{_e(p['avg_latency_ms'])}ms</td><td>{_e(p['status'])}</td></tr>"
        for p in data.get("providers", [])
    )
    empty = '<p class="muted">尚無觀測樣本。</p>' if not data.get("providers") else ""
    body = f"""<div class="card"><p class="muted">{_e(data.get('note', ''))}</p>
<table><tr><th>供應商</th><th>樣本數</th><th>成功率</th><th>平均延遲</th><th>狀態</th></tr>{rows}</table>{empty}</div>"""
    return _page(server, "供應商", body)


def _page_digests(server: DashboardServer) -> str:
    data = web_services.list_digests(server.root)
    rows = "".join(
        f"<tr><td>{_e(i['digest_id'][:8])}</td><td>{_e(i['type'])}</td><td>{_e(i['window_start'])}–{_e(i['window_end'])}</td>"
        f"<td>r{_e(i['revision'])}</td><td>{_e(i['status'])}</td><td>{_e(i['selected_count'])}/{_e(i['candidate_count'])}</td></tr>"
        for i in data.get("items", [])
    )
    body = f"""<div class="card">{_state_block(data.get('available', False), '尚無摘要紀錄。')}
<table><tr><th>digest</th><th>類型</th><th>窗口</th><th>版</th><th>狀態</th><th>選入/候選</th></tr>{rows}</table>
<p class="muted">預覽：POST /api/digest/preview；送出：POST /api/digest/send（需 digest_id+revision+payload_hash）。</p></div>"""
    return _page(server, "摘要", body)


def _page_notifications(server: DashboardServer) -> str:
    data = web_services.list_notifications(server.root)
    rows = "".join(
        f"<tr><td>{_e(i.get('event_type'))}</td><td>{_e(i.get('job'))}</td><td>{_e(i.get('channel'))}</td>"
        f"<td>{_e(i.get('provider'))}</td><td>{_e(i.get('status'))}</td><td>{_e(i.get('safe_error'))}</td></tr>"
        for i in data.get("items", [])
    )
    body = f"""<div class="card">{_state_block(data.get('available', False), '尚無通知紀錄。')}
<table><tr><th>事件</th><th>job</th><th>管道</th><th>供應商</th><th>狀態</th><th>錯誤</th></tr>{rows}</table></div>"""
    return _page(server, "通知", body)


def _page_tasks(server: DashboardServer) -> str:
    tasks = web_tasks.list_tasks(server.root, limit=100)
    rows = "".join(
        f"<tr><td>{_e(t['task_id'][:8])}</td><td>{_e(t['type'])}</td><td>{_e(t['status'])}</td>"
        f"<td>{_e(t['attempts'])}/{_e(t['max_attempts'])}</td><td>{_e(t['safe_error'])}</td><td>{_e(t['created_at'])}</td></tr>"
        for t in tasks
    )
    body = f"""<div class="card"><table><tr><th>task</th><th>類型</th><th>狀態</th><th>嘗試</th><th>錯誤</th><th>建立</th></tr>
{rows or '<tr><td colspan="6" class="muted">無工作。</td></tr>'}</table>
<p class="muted">POST 只排入工作；由 --serve-worker 執行。取消表示停止後續步驟，已送出的請求可能仍計費。</p></div>"""
    return _page(server, "工作", body)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="影片情報本機 Dashboard")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--auth-token", default=os.getenv("WEB_AUTH_TOKEN", ""))
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--trusted-proxy", action="store_true")
    args = parser.parse_args(argv)
    return serve(
        args.root.resolve(),
        host=args.host,
        port=args.port,
        auth_token=args.auth_token or None,
        allow_remote=args.allow_remote,
        trusted_proxy=args.trusted_proxy,
    )


if __name__ == "__main__":
    raise SystemExit(main())
