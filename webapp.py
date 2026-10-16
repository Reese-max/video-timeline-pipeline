"""Optional FastAPI Dashboard. No workers, database migrations, or model imports at startup."""
import asyncio
from collections import deque
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

import web_services as services


def server_options(host="127.0.0.1", port=8080, *, allow_remote=False, token=None, public_origin=None, trusted_proxy=None):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        parsed = urlsplit(public_origin or "")
        if (not allow_remote or not token or len(token) < 32 or parsed.scheme != "https" or not parsed.hostname
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password
                or not trusted_proxy):
            raise ValueError("Remote requires --allow-remote, a 32-character WEB_TOKEN, HTTPS public origin, and an exact trusted proxy IP")
        ipaddress.ip_address(trusted_proxy)
        return {"remote": True, "origins": {public_origin.rstrip("/")}, "trusted_proxy": trusted_proxy}
    address = f"[{host}]" if ":" in host else host
    return {"remote": False, "origins": {f"http://{address}:{port}", f"http://localhost:{port}"}, "trusted_proxy": None}


def create_app(root: Path, *, host="127.0.0.1", port=8080, enable_writes=False, token=None,
               allow_remote=False, public_origin=None, trusted_proxy=None):
    # Imports stay inside the optional entry point, keeping the normal CLI independent.
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from starlette.exceptions import HTTPException

    token = token if token is not None else os.getenv("WEB_TOKEN", "")
    options = server_options(host, port, allow_remote=allow_remote, token=token, public_origin=public_origin, trusted_proxy=trusted_proxy)
    if enable_writes and len(token) < 32:
        raise ValueError("Writes require a 32-character WEB_TOKEN")
    root = Path(root).resolve()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    template_dir = Path(__file__).with_name("web_templates")
    templates = Environment(loader=FileSystemLoader(template_dir), autoescape=select_autoescape(["html"]))
    origins = options["origins"]
    authorities = {urlsplit(origin).netloc for origin in origins}
    slots = asyncio.Semaphore(8)
    calls = deque()

    def response(data, status=200):
        return JSONResponse({"schema_version": 1, "state": data.get("state", "ready") if isinstance(data, dict) else "ready", "data": data}, status_code=status)

    def error(request, status, code):
        return JSONResponse({"schema_version": 1, "error": {"code": code, "safe_message": code,
            "retryable": status in {429, 503, 504}, "request_id": getattr(request.state, "request_id", uuid.uuid4().hex)}}, status_code=status)

    def authorized(request):
        value = request.headers.get("authorization", "")
        return bool(token and hmac.compare_digest(value.encode(), ("Bearer " + token).encode()))

    def read_session(request):
        value = request.cookies.get("dashboard_session", "")
        try:
            stamp, signature = value.split(".")
            valid_time = 0 <= time.time() - int(stamp) < 3600
            expected = hmac.new(token.encode(), stamp.encode(), hashlib.sha256).hexdigest()
            return bool(token and valid_time and hmac.compare_digest(signature, expected))
        except ValueError:
            return False

    @app.middleware("http")
    async def boundary(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        if (request.headers.get("host", "").lower() not in authorities
                or request.headers.get("origin", next(iter(origins))) not in origins
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return error(request, 403, "host_or_origin_rejected")
        if options["remote"]:
            if not request.client or request.client.host != options["trusted_proxy"]:
                return error(request, 403, "untrusted_proxy")
            if request.url.path not in {"/", "/static/dashboard.css", "/static/dashboard.js", "/api/session"} and not (authorized(request) or read_session(request)):
                return error(request, 401, "authentication_required")
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return error(request, 415, "json_required")
            preview = request.url.path == "/api/digest/preview" and not enable_writes
            if not preview and not authorized(request):
                return error(request, 401, "authentication_required")
            if request.url.path not in {"/api/digest/preview", "/api/session"} and not enable_writes:
                return error(request, 403, "read_only")
            now = time.monotonic()
            while calls and calls[0] < now - 60:
                calls.popleft()
            if len(calls) >= 30:
                return error(request, 429, "rate_limited")
            calls.append(now)
            try:
                length = int(request.headers.get("content-length", "0"))
                if not 0 <= length <= 16384:
                    return error(request, 413, "body_too_large")
                async def read_body():
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 16384:
                            return None
                    return bytes(body)
                body = await asyncio.wait_for(read_body(), timeout=5)
                if body is None:
                    return error(request, 413, "body_too_large")
                request._body = body
            except (ValueError, asyncio.TimeoutError):
                return error(request, 422, "invalid_request")
        try:
            if slots.locked():
                return error(request, 503, "busy")
            async with slots:
                result = await asyncio.wait_for(call_next(request), timeout=10)
        except asyncio.TimeoutError:
            return error(request, 504, "request_timeout")
        except Exception:
            return error(request, 500, "internal_error")
        result.headers.update({"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "X-Request-ID": request.state.request_id})
        return result

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return error(request, 422, "invalid_request")

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return error(request, 422, "invalid_request")

    @app.exception_handler(LookupError)
    async def not_found(request, exc):
        return error(request, 404, "not_found")

    @app.exception_handler(services.Unavailable)
    async def unavailable(request, exc):
        return error(request, 409 if exc.state == "migration_required" else 503, exc.state)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(request, exc.status_code, "not_found" if exc.status_code == 404 else "method_not_allowed")

    def filters(request):
        ignored = {"page", "page_size", "sort", "q", "limit"}
        values = {k: v for k, v in request.query_params.items() if k not in ignored}
        services.pipeline.library_filter_sql(values, events=True)
        return values

    def number(request, field, default, upper):
        try:
            value = int(request.query_params.get(field, str(default)))
            if not 1 <= value <= upper:
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise ValueError("Invalid pagination")

    @app.get("/api/health")
    def health():
        return response({"status": "ok", "mode": "write_enabled" if enable_writes else "read_only", "worker_started": False})

    @app.get("/api/stats")
    def stats(request: Request):
        return response(services.stats(root, window=request.query_params.get("window", "today"), timezone=request.query_params.get("timezone", "Asia/Taipei")))

    @app.get("/api/videos")
    def videos(request: Request):
        return response(services.videos(root, filters=filters(request), page=number(request, "page", 1, 100000),
                        page_size=number(request, "page_size", 50, 100), sort=request.query_params.get("sort", "title")))

    @app.get("/api/videos/{job}")
    def video(job: str, request: Request):
        return response(services.video_detail(root, job, page=number(request, "page", 1, 100000),
                        page_size=number(request, "page_size", 50, 100), kind=request.query_params.get("kind", "")))

    @app.get("/api/search")
    def search(request: Request):
        return response(services.search(root, request.query_params.get("q", ""), number(request, "limit", 20, 100), filters(request)))

    @app.get("/api/costs")
    def costs(request: Request):
        return response(services.costs(root, request.query_params.get("period", "daily")))

    @app.get("/api/budget-status")
    def budget():
        return response(services.costs(root).get("budget", {"state": "unavailable"}))

    @app.get("/api/jobs/failed")
    def failed():
        return response({"items": services.operations(root)["failed"]})

    @app.get("/api/operations/providers")
    def providers():
        return response(services.operations(root)["providers"])

    @app.get("/api/digests")
    def digests(request: Request):
        return response(services.history(root, "web_digest_drafts", number(request, "page", 1, 100000), number(request, "page_size", 50, 100)))

    @app.get("/api/notifications")
    def notifications(request: Request):
        return response(services.history(root, "notification_deliveries", number(request, "page", 1, 100000), number(request, "page_size", 50, 100)))

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return response(services.get_task(root, task_id))

    async def body(request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    @app.post("/api/session")
    def session(request: Request):
        stamp = str(int(time.time()))
        signature = hmac.new(token.encode(), stamp.encode(), hashlib.sha256).hexdigest()
        result = response({"state": "authorized"})
        result.set_cookie("dashboard_session", stamp + "." + signature, httponly=True, secure=options["remote"], samesite="strict", max_age=3600)
        return result

    @app.post("/api/digest/preview")
    async def preview(request: Request):
        value = await body(request)
        if set(value) - {"type", "date", "new_revision"} or not isinstance(value.get("new_revision", False), bool):
            raise ValueError("Invalid preview fields")
        return response(services.preview_digest(root, value.get("type", "daily"), value.get("date"), persist=enable_writes,
                                               new_revision=value.get("new_revision", False)))

    def enqueue(request, kind, payload):
        return response(services.enqueue_task(root, kind, payload, request.headers.get("Idempotency-Key")), 202)

    @app.post("/api/search")
    async def semantic(request: Request):
        return enqueue(request, "search", await body(request))

    @app.post("/api/ask")
    async def ask(request: Request):
        return enqueue(request, "ask", await body(request))

    @app.post("/api/digest/send")
    async def send(request: Request):
        return enqueue(request, "digest.send", await body(request))

    @app.post("/api/jobs/{job}/retry")
    async def retry(job: str, request: Request):
        value = await body(request)
        if set(value) != {"allow_cloud"}:
            raise ValueError("Explicit cloud opt-in required")
        return enqueue(request, "rss.retry", {"job": job, **value})

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return response(services.cancel_task(root, task_id))

    def render(request, page, data=None, state="ready"):
        return HTMLResponse(templates.get_template("dashboard.html").render(page=page, data=data, state=state,
            write_enabled=enable_writes, remote=options["remote"], authenticated=authorized(request) or read_session(request)))

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if options["remote"] and not (authorized(request) or read_session(request)):
            return render(request, "登入", state="disabled")
        try:
            return render(request, "總覽", services.stats(root, window=request.query_params.get("window", "today"),
                          timezone=request.query_params.get("timezone", "Asia/Taipei")))
        except (services.Unavailable, RuntimeError, sqlite3.Error):
            return render(request, "總覽", state="migration_required")

    @app.get("/videos", response_class=HTMLResponse)
    def library(request: Request):
        try:
            return render(request, "影片資料庫", services.videos(root, filters=filters(request), page=number(request, "page", 1, 100000),
                          page_size=number(request, "page_size", 50, 100), sort=request.query_params.get("sort", "title")))
        except services.Unavailable:
            return render(request, "影片資料庫", state="migration_required")

    @app.get("/videos/{job}", response_class=HTMLResponse)
    def detail(job: str, request: Request):
        return render(request, "影片證據", services.video_detail(root, job, page=number(request, "page", 1, 100000),
                       page_size=number(request, "page_size", 50, 100), kind=request.query_params.get("kind", "")))

    @app.get("/operations", response_class=HTMLResponse)
    @app.get("/operations/failed", response_class=HTMLResponse)
    @app.get("/operations/providers", response_class=HTMLResponse)
    def operation_page(request: Request):
        return render(request, "工作與用量", services.operations(root))

    @app.get("/costs", response_class=HTMLResponse)
    def cost_page(request: Request):
        return render(request, "成本與預算", services.costs(root))

    @app.get("/digests", response_class=HTMLResponse)
    @app.get("/notifications", response_class=HTMLResponse)
    def digest_page(request: Request):
        return render(request, "摘要與通知", services.history(root, "web_digest_drafts"))

    app.mount("/static", StaticFiles(directory=Path(__file__).with_name("web_static")), name="static")
    return app


def serve(root, **options):
    import uvicorn
    host, port = options.get("host", "127.0.0.1"), options.get("port", 8080)
    app = create_app(root, **options)
    uvicorn.run(app, host=host, port=port, proxy_headers=False, access_log=False, timeout_keep_alive=5,
                timeout_graceful_shutdown=10, limit_concurrency=16)
