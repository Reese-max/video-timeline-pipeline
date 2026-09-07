#!/usr/bin/env python3
"""Fetch Instagram / X / Threads via Bright Data — MCP HTTP first; Datasets REST IG-only fallback."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

JOBS = Path("/workspace/video-timeline-pipeline/jobs")
DATASET_ID = "gd_lyclm20il4r5helnj"
API_ROOT = "https://api.brightdata.com/datasets/v3"
MCP_HOST = "https://mcp.brightdata.com/mcp"
SECRETS = (
    Path("/home/box/agent-data/box-secrets.json"),
    Path("/home/box/sand-data/box-secrets.json"),
)
SNAPSHOT_RE = re.compile(r"^s[a-z]*_[A-Za-z0-9_-]+$")


def redact(tok: str, text: str) -> str:
    if not tok:
        return text
    return text.replace(tok, tok[:4] + "…")


def token() -> str:
    for key in ("BRIGHTDATA_MCP_TOKEN", "BRIGHTDATA_API_TOKEN"):
        t = (os.environ.get(key) or "").strip()
        if t:
            return t
    for p in SECRETS:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            sec = data.get("secrets") if isinstance(data, dict) else {}
            if isinstance(sec, dict):
                for key in ("BRIGHTDATA_MCP_TOKEN", "BRIGHTDATA_API_TOKEN"):
                    v = (sec.get(key) or "").strip()
                    if v:
                        return v
        except Exception:
            pass
    envp = Path("/workspace/video-timeline-pipeline/.env")
    if envp.exists():
        for line in envp.read_text().splitlines():
            if line.startswith("BRIGHTDATA_API_TOKEN=") or line.startswith("BRIGHTDATA_MCP_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def detect_platform(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("instagram.com", "www.instagram.com") or host.endswith(".instagram.com"):
        return "instagram"
    if host in ("x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "t.co"):
        return "x"
    if host in ("threads.net", "www.threads.net") or host.endswith(".threads.net"):
        return "threads"
    # path-based fallbacks
    low = (url or "").lower()
    if "instagram.com" in low:
        return "instagram"
    if "twitter.com" in low or "x.com/" in low or low.startswith("https://x.com"):
        return "x"
    if "threads.net" in low:
        return "threads"
    return ""


def content_id(url: str, platform: str) -> str:
    u = url or ""
    if platform == "instagram":
        m = re.search(r"/(?:reel|reels|p)/([^/?#]+)", u, re.I)
        return m.group(1) if m else ""
    if platform == "x":
        m = re.search(r"/status/(\d+)", u, re.I)
        return m.group(1) if m else ""
    if platform == "threads":
        m = re.search(r"/post/([\w-]+)", u, re.I)
        return m.group(1) if m else ""
    return ""


def normalize_url(url: str, platform: str, cid: str) -> str:
    if platform == "instagram" and cid:
        return f"https://www.instagram.com/reel/{cid}/"
    return (url or "").split("?")[0]


def provider_label(platform: str) -> str:
    return {
        "instagram": "brightdata-ig",
        "x": "brightdata-x",
        "threads": "brightdata-threads",
    }.get(platform, "brightdata")


def http_json(method: str, url: str, tok: str, body=None, timeout: int = 90):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")[:800]
        raise RuntimeError(f"http_{e.code}:{redact(tok, err)}") from e


def parse_sse_jsonrpc(raw: str):
    """Extract JSON-RPC payloads from streamable-HTTP SSE body."""
    payloads = []
    for line in (raw or "").splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                payloads.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue
    if payloads:
        return payloads
    try:
        return [json.loads(raw)]
    except Exception:
        return []


def mcp_post(url: str, body: dict, headers: dict, timeout: int = 120):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="ignore")
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        return r.status, hdrs, raw


def extract_json_blob(text: str):
    """Pull first JSON array/object from MCP tool text (may include security notice)."""
    if not text:
        return None
    text = text.strip()
    for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        for matcher in re.finditer(pattern, text):
            cand = matcher.group(0)
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except Exception:
        return None


def mcp_tool_call(tok: str, tool_name: str, arguments: dict, client_name: str = "irisx-brightdata"):
    """MCP initialize → session → tools/call. Returns (result_dict_or_list, raw_text_parts)."""
    groups = "social" if tool_name.startswith("web_data_") else "advanced_scraping,social"
    mcp_url = f"{MCP_HOST}?{urllib.parse.urlencode({'token': tok, 'groups': groups})}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1.1"},
        },
    }
    status, hdrs, raw = mcp_post(mcp_url, init, headers, timeout=45)
    session_id = hdrs.get("mcp-session-id")
    if not session_id:
        raise RuntimeError(f"mcp_no_session:status={status}")
    headers2 = dict(headers)
    headers2["mcp-session-id"] = session_id
    try:
        mcp_post(
            mcp_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers2,
            timeout=30,
        )
    except Exception:
        pass
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    status, hdrs, raw = mcp_post(mcp_url, call, headers2, timeout=180)
    payloads = parse_sse_jsonrpc(raw)
    if not payloads:
        raise RuntimeError(f"mcp_empty_response:status={status}")
    chosen = None
    for p in payloads:
        if isinstance(p, dict) and ("result" in p or "error" in p):
            chosen = p
    if chosen is None:
        chosen = payloads[-1]
    if chosen.get("error"):
        err = chosen["error"]
        raise RuntimeError(f"mcp_rpc_error:{redact(tok, json.dumps(err)[:400])}")
    result = chosen.get("result") or {}
    return result


def mcp_content_texts(result) -> list[str]:
    texts: list[str] = []
    if not isinstance(result, dict):
        return texts
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                texts.append(str(item["text"]))
            elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
                res = item["resource"]
                if res.get("text"):
                    texts.append(str(res["text"]))
    return texts


def mcp_records_from_result(result) -> list:
    records: list = []
    texts = mcp_content_texts(result)
    for text in texts:
        blob = extract_json_blob(text)
        if isinstance(blob, list):
            records.extend([x for x in blob if isinstance(x, dict)])
        elif isinstance(blob, dict):
            records.append(blob)
    if isinstance(result, list):
        records.extend([x for x in result if isinstance(x, dict)])
    if isinstance(result, dict) and (
        result.get("description")
        or result.get("shortcode")
        or result.get("video_url")
        or result.get("text")
        or result.get("full_text")
        or result.get("tweet_id")
        or result.get("id")
    ):
        # avoid double-adding empty shells tha