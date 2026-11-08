"""Validate social URLs and match provider records to a concrete post identity."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit


def platform_for_url(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        if parts.scheme.lower() not in {"http", "https"} or parts.username is not None or parts.password is not None:
            return ""
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
        if port is not None and port != (443 if parts.scheme.lower() == "https" else 80):
            return ""
    except (ValueError, TypeError):
        return ""
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host in {"x.com", "twitter.com", "t.co"} or host.endswith((".x.com", ".twitter.com")):
        return "x"
    if host in {"threads.net", "threads.com"} or host.endswith((".threads.net", ".threads.com")):
        return "threads"
    if host in {"facebook.com", "fb.watch", "fb.gg"} or host.endswith(".facebook.com"):
        return "facebook"
    if host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com"):
        return "youtube"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host in {"bilibili.com", "b23.tv"} or host.endswith(".bilibili.com"):
        return "bilibili"
    if host == "douyin.com" or host.endswith(".douyin.com"):
        return "douyin"
    if host == "vimeo.com" or host.endswith(".vimeo.com"):
        return "vimeo"
    return ""


def content_id(url: str, platform: str) -> str:
    if platform_for_url(url) != platform:
        return ""
    try:
        path = unquote(urlsplit(url).path)
    except (ValueError, TypeError):
        return ""
    patterns = {
        "instagram": r"/(?:reel|reels|p)/([\w-]+)(?:/|$)",
        "x": r"/status/(\d+)(?:/|$)",
        "threads": r"/(?:post|t)/([\w-]+)(?:/|$)",
    }
    pattern = patterns.get(platform)
    match = re.search(pattern, path, re.I) if pattern else None
    return match.group(1) if match else ""


def video_identity(url: str) -> tuple[str, str] | None:
    platform = platform_for_url(url)
    if not platform:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = unquote(parts.path)
    if platform in {"instagram", "x", "threads"}:
        identifier = content_id(url, platform)
        if not identifier and host == "t.co":
            match = re.fullmatch(r"/([A-Za-z0-9_-]+)/?", path)
            identifier = "short:" + match.group(1) if match else ""
        return (platform, identifier) if identifier else None
    patterns = {
        "facebook": r"/(?:videos|reel|reels)/([\w-]+)(?:/|$)|/share/(?:v|r)/([\w-]+)(?:/|$)",
        "youtube": r"/(?:shorts|embed|live)/([\w-]{6,})(?:/|$)",
        "tiktok": r"/video/(\d+)(?:/|$)",
        "bilibili": r"/video/(BV[\w]+|av\d+)(?:/|$)",
        "douyin": r"/video/(\d+)(?:/|$)",
        "vimeo": r"/(\d+)(?:/|$)",
    }
    match = re.search(patterns[platform], path, re.I)
    identifier = next((part for part in match.groups() if part), "") if match else ""
    if platform == "youtube" and host == "youtu.be":
        short = re.fullmatch(r"/([\w-]{6,})/?", path)
        identifier = short.group(1) if short else ""
    if (platform == "youtube" and path.rstrip("/") == "/watch") or (platform == "facebook" and path.rstrip("/") == "/watch"):
        values = parse_qs(parts.query).get("v", [])
        if len(values) == 1 and re.fullmatch(r"[\w-]{6,}" if platform == "youtube" else r"\d+", values[0]):
            identifier = values[0]
    if host in {"fb.watch", "fb.gg", "vm.tiktok.com", "vt.tiktok.com", "b23.tv"}:
        short = re.fullmatch(r"/([\w-]+)/?", path)
        identifier = "short:" + short.group(1) if short else ""
    return (platform, identifier) if identifier else None


def provider_item_matches(item, wanted: str, platform: str) -> bool:
    if not isinstance(item, dict) or not wanted:
        return False
    matched_url = False
    for field in ("url", "webpage_url", "postUrl", "post_url"):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            continue
        source_platform = platform_for_url(value)
        source_id = content_id(value, source_platform) if source_platform else ""
        if source_platform and source_id:
            if source_platform != platform or source_id != wanted:
                return False
            matched_url = True
    fields = {
        "instagram": ("shortcode", "shortCode", "postCode", "code"),
        "x": ("tweet_id", "post_id", "postId", "id"),
        "threads": ("post_id", "postId", "id"),
    }.get(platform, ())
    ids = [str(item[field]) for field in fields if item.get(field) not in (None, "")]
    if ids and any(identifier != wanted for identifier in ids):
        return False
    return matched_url or bool(ids) or (platform == "instagram" and str(item.get("id") or "") == wanted)
