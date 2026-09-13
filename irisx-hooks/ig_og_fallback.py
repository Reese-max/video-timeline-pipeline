#!/usr/bin/env python3
"""Best-effort Instagram metadata when yt-dlp fails (embed/oembed/UA)."""
from __future__ import annotations
import json, re, sys, urllib.request, html, codecs

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def decode_html(raw: bytes) -> str:
    # Prefer charset from meta; fallback utf-8
    head = raw[:2000].decode("ascii", "ignore")
    m = re.search(r"charset=([\w-]+)", head, re.I)
    enc = (m.group(1) if m else "utf-8").lower()
    try:
        return raw.decode(enc, "strict")
    except Exception:
        return raw.decode("utf-8", "replace")


def fix_mojibake(s: str) -> str:
    if not s:
        return s
    # classic utf-8 read as latin1
    if re.search(r"[ÃÂæåäö]|ç.|è.|é.", s):
        try:
            fixed = s.encode("latin1", "ignore").decode("utf-8")
            if fixed.count("�") < s.count("�"):
                s = fixed
        except Exception:
            pass
    # drop leftover replacement / mojibake-heavy strings
    return s


def looks_garbage(s: str) -> bool:
    if not s or len(s) < 3:
        return True
    bad = sum(1 for ch in s if ch == "�" or ord(ch) > 0xFFFF)
    # lots of latin garbage syllables typical of mojibake
    if re.search(r"æ.|ç.|å.|é.|Ã.", s) and not re.search(r"[\u4e00-\u9fff]", s):
        return True
    return bad > max(2, len(s) // 20)


def main(url: str) -> dict:
    out = {"url": url, "ok": False, "title": "", "author": "", "caption": "", "note": ""}
    m = re.search(r"/reel/([^/?#]+)", url) or re.search(r"/p/([^/?#]+)", url)
    code = m.group(1) if m else ""
    out["shortcode"] = code
    candidates = []
    if code:
        candidates += [
            f"https://www.instagram.com/reel/{code}/embed/captioned/",
            f"https://www.instagram.com/p/{code}/embed/",
            f"https://www.instagram.com/reel/{code}/",
        ]
    candidates.append(url)
    text = ""
    for u in candidates:
        try:
            text = decode_html(fetch(u))
            if "may be broken" in text and len(text) < 80000:
                continue
            if len(text) > 1000:
                break
        except Exception as e:
            out["note"] = str(e)
    if not text:
        out["note"] = out.get("note") or "fetch_failed"
        print(json.dumps(out, ensure_ascii=False))
        return out
    for pat in [
        r'class="Caption"[^>]*>(.*?)</div>',
        r'"edge_media_to_caption"[^\}]{0,200}"text":"((?:\\.|[^"\\])*)"',
        r'"caption":"((?:\\.|[^"\\])*)"',
        r'property="og:description" content="([^"]+)"',
        r'property="og:title" content="([^"]+)"',
    ]:
        m = re.search(pat, text, re.S | re.I)
        if not m:
            continue
        val = m.group(1)
        val = re.sub(r"<[^>]+>", " ", val)
        try:
            val = codecs.decode(val, "unicode_escape")
        except Exception:
            try:
                val = bytes(val, "utf-8").decode("unicode_escape")
            except Exception:
                pass
        val = fix_mojibake(html.unescape(re.sub(r"\s+", " ", val)).strip())
        if looks_garbage(val) or "may be broken" in val.lower():
            continue
        if "caption" in pat or "description" in pat:
            out["caption"] = val[:1500]
        else:
            out["title"] = val[:200]
    um = re.search(r'"username":"([^"]+)"', text)
    if um and not looks_garbage(um.group(1)):
        out["author"] = um.group(1)
    # Also try owner from title patterns like "username on Instagram"
    if not out["author"]:
        tm = re.search(r"([A-Za-z0-9._]{2,30})\s+on Instagram", text)
        if tm:
            out["author"] = tm.group(1)
    if out["caption"] or out["title"]:
        out["ok"] = True
    else:
        out["note"] = "instagram_blocked_or_login_wall"
        out["ok"] = False
    print(json.dumps(out, ensure_ascii=False))
    return out

if __name__ == "__main__":
    main(sys.argv[1])
