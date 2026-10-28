#!/usr/bin/env python3
"""Opt-in YouTube proxy probe; never logs proxy credentials."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.parse import unquote, urlsplit
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import pipeline
import notifications


def probe(proxy: str) -> bool:
    try:
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        deadline = time.monotonic() + 8
        with socket.create_connection((parsed.hostname, port), timeout=3) as tcp:
            with closing(ssl.create_default_context().wrap_socket(tcp, server_hostname=parsed.hostname)
                         if parsed.scheme == "https" else tcp) as conn:
                auth = ""
                if parsed.username is not None:
                    credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
                    auth = "Proxy-Authorization: Basic " + base64.b64encode(credentials.encode()).decode() + "\r\n"
                conn.sendall(("CONNECT www.youtube.com:443 HTTP/1.1\r\n"
                              "Host: www.youtube.com:443\r\n" + auth + "\r\n").encode("ascii"))
                header = b""
                while b"\r\n\r\n" not in header and len(header) < 8192:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    conn.settimeout(min(3, remaining))
                    chunk = conn.recv(min(1024, 8192 - len(header)))
                    if not chunk:
                        return False
                    header += chunk
                status = header.split(b"\r\n", 1)[0].split()
                return (b"\r\n\r\n" in header and len(status) >= 2
                        and status[0] in {b"HTTP/1.0", b"HTTP/1.1"} and status[1] == b"200")
    except (OSError, ValueError, UnicodeError):
        return False


def notify(root: Path, status: str, transition_id: str) -> dict[str, str]:
    """Reuse the notification outbox, targeted claim, provider and delivery audit."""
    webhook = next((os.getenv(key, "").strip() for key in
                    ("DISCORD_WEBHOOK_URL", "NOTIFY_WEBHOOK_URL", "RSS_NOTIFY_WEBHOOK")
                    if os.getenv(key, "").strip()), "")
    provider_name = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    recipient = webhook or os.getenv("EMAIL_TO", "").strip()
    if not recipient or (not webhook and
                         (provider_name not in {"smtp", "resend"} or not os.getenv("EMAIL_FROM")
                          or not os.getenv("SMTP_HOST" if provider_name == "smtp" else "RESEND_API_KEY"))):
        print("未完整設定通知 provider／收件者；略過通知。")
        return {"stage": "configuration", "status": "skipped"}
    text = "YouTube Proxy 已恢復正常。" if status == "healthy" else "YouTube Proxy 連續檢查失敗，請檢查家用代理連線。"
    stage = "provider"
    try:
        notifications.bind_cost_root(root)
        provider = notifications.get_configured_provider("webhook" if webhook else provider_name)
        stage = "outbox"
        with closing(notifications.get_operations_db(root)) as conn:
            event_id = notifications.record_event(conn, "proxy." + status, None, None, None,
                                                  {"status": status, "text": text}, transition_id)
            delivery_id = notifications.enqueue_delivery(conn, event_id, "webhook" if webhook else "email",
                                                         recipient, provider.__class__.__name__, transition_id)
            stage = "claim"
            claimed = notifications.claim_pending_deliveries(conn, f"proxy_{os.getpid()}", limit=1,
                                                             delivery_id=delivery_id)
            if not claimed:
                print("通知未取得 delivery lease；略過通知。", file=sys.stderr)
                return {"stage": stage, "status": "skipped"}
            target = claimed[0]
            started = notifications._utc_now_iso()
            stage = "send"
            diagnostic = {"stage": stage}
            try:
                result = provider.send(recipient=recipient, subject="【YouTube Proxy】連線狀態變更",
                                       text_body=text, html_body=None,
                                       idempotency_key=target["provider_idempotency_key"])
            except Exception as error:
                diagnostic["error_type"] = type(error).__name__
                print(f"通知失敗（階段：{stage}；{type(error).__name__}）。", file=sys.stderr)
                result = notifications.DeliveryResult(status="unknown", safe_error="請人工核對通知是否送達")
            stage = "delivery_audit"
            notifications.update_delivery_status(conn, delivery_id, target["lease_token"], result, 1, started)
            diagnostic["status"] = result.status
            print(f"通知結果（階段：send）：{result.status}",
                  file=sys.stdout if result.status == "accepted" else sys.stderr)
            return diagnostic
    except Exception as error:
        # Exception text may contain credentials; persist only the stage and type.
        print(f"通知失敗（階段：{stage}；{type(error).__name__}）。", file=sys.stderr)
        return {"stage": stage, "status": "error", "error_type": type(error).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="檢查 YouTube Proxy，僅於狀態變更時通知。")
    parser.add_argument("--dry-run", action="store_true", help="只預覽通知，不寫入狀態或寄送")
    args = parser.parse_args(argv)
    pipeline.load_dotenv(REPO / ".env")
    proxy = os.getenv("YTDLP_YOUTUBE_PROXY", "").strip()
    if not proxy:
        print("未設定 YTDLP_YOUTUBE_PROXY。")
        return 2
    healthy = probe(proxy)
    code = 0 if healthy else 1
    print("YouTube Proxy：" + ("正常" if healthy else "異常"))
    try:
        raw = os.getenv("YOUTUBE_PROXY_HEALTH_FAILURES", "2").strip()
        if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
            raise ValueError("失敗門檻必須是正整數")
        threshold = int(raw)
        path = Path(os.getenv("YOUTUBE_PROXY_HEALTH_STATE", "").strip()
                    or "~/.local/state/video-timeline-pipeline/youtube-proxy.json").expanduser()
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "healthy", "failures": 0}
        if state["status"] not in {"healthy", "unhealthy"} or type(state["failures"]) is not int or state["failures"] < 0:
            raise ValueError("狀態檔格式無效")
        failures = 0 if healthy else min(state["failures"] + 1, threshold)
        status = "healthy" if healthy else "unhealthy" if failures >= threshold else state["status"]
        changed = status != state["status"]
        if args.dry_run:
            print(f"預覽：連續失敗 {failures}/{threshold}；" +
                  (f"將通知狀態變更為 {status}。" if changed else "沒有狀態變更，不通知。"))
            return code
        # Persist before dispatch: an uncertain send must never be automatically duplicated.
        state.update(status=status, failures=failures)
        pipeline.write_json(path, state)
        if changed:
            state["notification"] = notify(path.parent, status, "proxy:" + uuid.uuid4().hex)
            pipeline.write_json(path, state)
    except Exception as error:
        # Never print exception details: provider/URL errors may contain credentials.
        print(f"無法保存狀態或通知（{type(error).__name__}）；仍回傳連線檢查結果。", file=sys.stderr)
    return code


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
