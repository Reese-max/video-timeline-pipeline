#!/usr/bin/env python3
"""
IRISX video takeaway cascade — always try to produce a usable draft.

Levels:
  1. link-summarize / video-pipeline (yt-dlp + ASR notes)
  2. Bright Data social (IG reels / X posts / Threads markdown; BRIGHTDATA_* tokens) — preferred
  3. Apify Instagram reel scraper (APIFY_TOKEN spare)
  4. Clean OG/embed preview (no mojibake)
  5. Honest failure (never invent content, never reuse wrong job)

Stdout: JSON {ok, level, url, job, draft, keys, note}
Also used by pre_llm_call hook.
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

ROOT = Path("/home/box/irisx-failover-restore/20260906/profile-irisx/hooks")
JOBS = Path("/workspace/video-timeline-pipeline/jobs")
LOG = Path("/home/box/irisx-failover-restore/20260906/runtime/video-hook")
RENDER = ROOT / "render_takeaway.py"
APIFY = ROOT / "apify_ig_fallback.py"
BRIGHTDATA = ROOT / "brightdata_ig_fallback.py"
OG = ROOT / "ig_og_fallback.py"
LINK = Path("/workspace/bin/link-summarize.sh")

GENERIC = {
    "facebook", "instagram", "youtube", "youtu", "tiktok", "share", "watch",
    "mibextid", "wwxifr", "https", "http", "www", "com", "reels", "reel",
    "videos", "video", "posts", "story", "stories", "channel", "user",
    "twitter", "threads", "status", "mobile",
}


def video_keys(url: str) -> set[str]:
    keys = set()
    u = unquote(url or "")
    for pat in (
        r"/share/v/([^/?#\s]+)",
        r"/share/r/([^/?#\s]+)",
        r"/reel(?:s)?/([^/?#\s]+)",
        r"/videos/(\d+)",
        r"[?&]v=(\d{6,})",
        r"youtu\.be/([\w-]{6,})",
        r"youtube\.com/watch\?[^\s]*v=([\w-]{6,})",
        r"tiktok\.com/@[^/]+/video/(\d+)",
        r"vm\.tiktok\.com/([\w]+)",
        r"fb\.watch/([\w-]+)",
        r"/p/([^/?#\s]+)",
        r"/status/(\d+)",
        r"threads\.net/@[^/]+/post/([\w-]+)",
        r"/post/([\w-]{6,})",
    ):
        for m in re.finditer(pat, u, re.I):
            k = m.group(1).strip()
            if not k or k.lower() in GENERIC:
                continue
            # X status ids are numeric (may be short on legacy posts); others need len>=5
            if k.isdigit() and len(k) >= 2:
                keys.add(k)
            elif len(k) >= 5:
                keys.add(k)
    try:
        qs = parse_qs(urlparse(u).query)
        for v in qs.get("v", []):
            if v.isdigit() and len(v) >= 6:
                keys.add(v)
        seg = (urlparse(u).path or "").rstrip("/").split("/")[-1]
        if seg and seg.lower() not in GENERIC and re.match(r"^[\w-]{8,}$", seg):
            keys.add(seg)
    except Exception:
        pass
    return keys


def job_blob(job: Path) -> str:
    parts = []
    for name in ("source.json", "source.info.json", "knowledge.md", "notes.md", "request_url.txt", "apify.item.json", "brightdata.item.json"):
        p = job / name
        if p.exists():
            try:
                parts.append(p.read_text(errors="ignore")[:80000])
            except Exception:
                pass
    return "\n".join(parts)


def job_matches(job: Path, url: str) -> bool:
    keys = video_keys(url)
    blob = job_blob(job)
    if not keys:
        return url.split("?")[0] in blob or url in blob
    return any(k in blob for k in keys)


def find_job(url: str) -> Path | None:
    if not JOBS.is_dir():
        return None
    matched = []
    for d in JOBS.iterdir():
        if not d.is_dir() or not (d / "notes.md").exists():
            continue
        man = d / "manifest.json"
        if man.exists():
            try:
                if json.loads(man.read_text()).get("status") == "failed":
                    continue
            except Exception:
                pass
        if job_matches(d, url):
            matched.append(((d / "notes.md").stat().st_mtime, d))
    if not matched:
        return None
    matched.sort(key=lambda t: -t[0])
    return matched[0][1]


def render(job: Path) -> str:
    try:
        p = subprocess.run([sys.executable, str(RENDER), str(job)], capture_output=True, text=True, timeout=30)
        return (p.stdout or "").strip()
    except Exception as e:
        return f"(render failed: {e})"


def level1_pipeline(url: str) -> Path | None:
    if not LINK.exists():
        return None
    try:
        subprocess.run([str(LINK), url], capture_output=True, text=True, timeout=540, cwd="/workspace/video-timeline-pipeline")
    except Exception as e:
        (LOG / "last-run.log").write_text(f"level1 error: {e}")
        return None
    return find_job(url)



def _brightdata_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        h in u
        for h in (
            "instagram.com",
            "x.com/",
            "://x.com",
            "twitter.com",
            "mobile.twitter.com",
            "threads.net",
        )
    )


def level2_brightdata(url: str) -> Path | None:
    if not _brightdata_url(url):
        return None
    if not BRIGHTDATA.exists():
        return None
    try:
        p = subprocess.run([sys.executable, str(BRIGHTDATA), url], capture_output=True, text=True, timeout=240)
        (LOG / "last-brightdata.json").write_text((p.stdout or "")[-20000:] + "\n" + (p.stderr or "")[-4000:])
        line = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else "{}"
        meta = json.loads(line)
    except Exception as e:
        (LOG / "last-brightdata.json").write_text(f'{{"ok":false,"note":"{e}"}}')
        return None
    if meta.get("ok") and meta.get("job"):
        job = Path(meta["job"])
        try:
            (job / "request_url.txt").write_text(url + "\n")
        except Exception:
            pass
        if job_matches(job, url) or not video_keys(url):
            return job if (job / "notes.md").exists() else None
        keys = video_keys(url)
        if any(k in job.name for k in keys):
            return job if (job / "notes.md").exists() else None
    return None


def level2_apify(url: str) -> Path | None:
    if "instagram.com" not in url:
        return None
    if not APIFY.exists():
        return None
    try:
        p = subprocess.run([sys.executable, str(APIFY), url], capture_output=True, text=True, timeout=420)
        (LOG / "last-apify.json").write_text((p.stdout or "")[-20000:])
        line = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else "{}"
        meta = json.loads(line)
    except Exception as e:
        (LOG / "last-apify.json").write_text(f'{{"ok":false,"note":"{e}"}}')
        return None
    if meta.get("ok") and meta.get("job"):
        job = Path(meta["job"])
        try:
            (job / "request_url.txt").write_text(url + "\n")
        except Exception:
            pass
        # If direct URL miss, apify script may still return a job — verify keys
        if job_matches(job, url) or not video_keys(url):
            return job if (job / "notes.md").exists() else None
        # accept if shortcode folder name matches
        keys = video_keys(url)
        if any(k in job.name for k in keys):
            return job if (job / "notes.md").exists() else None
    return None


def level3_og(url: str) -> str:
    """Return a draft string from OG, or empty."""
    if "instagram.com" not in url and "facebook.com" not in url:
        return ""
    if not OG.exists():
        return ""
    try:
        p = subprocess.run([sys.executable, str(OG), url], capture_output=True, text=True, timeout=40)
        meta = json.loads(p.stdout.strip() or "{}")
    except Exception:
        return ""
    cap = (meta.get("caption") or meta.get("title") or "").strip()
    if not meta.get("ok") or not cap:
        return ""
    # reject mojibake
    if "�" in cap or (("æ" in cap or "ç" in cap) and not any("\u4e00" <= ch <= "\u9fff" for ch in cap)):
        return ""
    author = meta.get("author") or "（未知）"
    return (
        "哼～這支我只抽到預覽，不是完整影音分析喔\n"
        f"類型：待補（預覽）\n\n一句話\n{cap[:180]}～\n\n"
        f"影片資訊\n作者：{author}\nURL: {url}\n\n"
        "可以帶走的重點\n- 以上是頁面預覽，不是完整分析\n"
        "- 若要完整可帶走，下次會再走 Bright Data／Apify／管線\n"
    )


def cascade(url: str) -> dict:
    LOG.mkdir(parents=True, exist_ok=True)
    keys = sorted(video_keys(url))
    out = {"ok": False, "level": 0, "url": url, "job": "", "draft": "", "keys": keys, "note": ""}

    # L1
    job = level1_pipeline(url)
    if job and (job / "notes.md").exists():
        draft = render(job)
        if draft and "render failed" not in draft:
            out.update(ok=True, level=1, job=str(job), draft=draft, note="video-pipeline")
            return out

    # L2 Bright Data IG/X/Threads (preferred over Apify)
    job = level2_brightdata(url)
    if job and (job / "notes.md").exists():
        draft = render(job)
        if draft:
            # Infer platform note from job provider if present
            note = "brightdata-social"
            try:
                info = json.loads((job / "source.info.json").read_text())
                note = info.get("provider") or info.get("platform") or note
                if note in ("instagram", "x", "threads"):
                    note = f"brightdata-{note if note != 'instagram' else 'ig'}"
            except Exception:
                low = (url or "").lower()
                if "instagram.com" in low:
                    note = "brightdata-ig"
                elif "threads.net" in low:
                    note = "brightdata-threads"
                elif "twitter.com" in low or "x.com" in low:
                    note = "brightdata-x"
            out.update(ok=True, level=2, job=str(job), draft=draft, note=note)
            return out

    # L3 Apify IG (spare)
    job = level2_apify(url)
    if job and (job / "notes.md").exists():
        draft = render(job)
        if draft:
            out.update(ok=True, level=3, job=str(job), draft=draft, note="apify-instagram")
            return out

    # L4 OG preview
    draft = level3_og(url)
    if draft:
        out.update(ok=True, level=4, draft=draft, note="og-preview")
        return out

    # L5 honest fail
    out.update(
        ok=False,
        level=5,
        draft=(
            f"哼～這一支（{', '.join(keys) or 'unknown'}）我這邊抓不到可用內容，不能亂編喔。\n"
            "已試過：管線下載、Bright Data（IG／X／Threads）、Apify（若有 token）、頁面預覽。\n"
            "禁止拿上一支舊影片充數。\n"
        ),
        note="all_levels_failed",
    )
    return out


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print(json.dumps({"ok": False, "note": "usage: video_takeaway_cascade.py <url>"}))
        raise SystemExit(2)
    print(json.dumps(cascade(url), ensure_ascii=False))
