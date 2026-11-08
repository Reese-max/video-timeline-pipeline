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
from irisx_identity import content_id, platform_for_url, provider_item_matches, video_identity

ROOT = Path("/home/box/irisx-failover-restore/20260906/profile-irisx/hooks")
JOBS = Path("/workspace/video-timeline-pipeline/jobs")
LOG = Path("/home/box/irisx-failover-restore/20260906/runtime/video-hook")
RENDER = ROOT / "render_takeaway.py"
APIFY = ROOT / "apify_ig_fallback.py"
BRIGHTDATA = ROOT / "brightdata_ig_fallback.py"
OG = ROOT / "ig_og_fallback.py"
LINK = Path("/workspace/bin/link-summarize.sh")

def video_keys(url: str) -> set[str]:
    identity = video_identity(url)
    return {identity[1]} if identity else set()


def job_matches(job: Path, url: str) -> bool:
    wanted = video_identity(url)
    if not wanted:
        return False
    observed = []
    for name in ("manifest.json", "source.json", "source.info.json"):
        path = job / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        urls = []
        if name == "source.info.json":
            urls = [data.get("webpage_url"), data.get("original_url")]
        else:
            source = data.get("source")
            if isinstance(source, dict):
                urls.append(source.get("url"))
            if name == "source.json":
                urls.append(data.get("url"))
        present = [value for value in urls if isinstance(value, str) and value]
        for value in present:
            identity = video_identity(value)
            if not identity:
                return False
            observed.append(identity)
        if name == "source.info.json" and not present and data.get("id"):
            extractor = str(data.get("extractor_key") or data.get("extractor") or "").lower()
            platforms = {"instagram": "instagram", "youtube": "youtube", "facebook": "facebook",
                         "tiktok": "tiktok", "twitter": "x", "threads": "threads", "vimeo": "vimeo"}
            platform = next((value for prefix, value in platforms.items() if extractor.startswith(prefix)), "")
            if platform:
                observed.append((platform, str(data["id"])))
    return bool(observed) and all(identity == wanted for identity in observed)


def provider_job_matches(job: Path, url: str, provider: str) -> bool:
    platform = platform_for_url(url)
    cid = content_id(url, platform)
    if not cid or provider not in {"brightdata", "apify"}:
        return False
    if provider == "apify" and platform != "instagram":
        return False
    if provider == "brightdata" and platform not in {"instagram", "x", "threads"}:
        return False
    try:
        resolved = job.resolve(strict=True)
        resolved.relative_to(JOBS.resolve(strict=True))
        if resolved.name != f"{provider}-{cid}" or not (resolved / "notes.md").is_file():
            return False
        item = json.loads((resolved / f"{provider}.item.json").read_text(encoding="utf-8"))
        if not provider_item_matches(item, cid, platform):
            return False
        if platform == "threads":
            return video_identity(item.get("_verified_source_url")) == video_identity(url)
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


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
        provider = next((p for p in ("brightdata", "apify") if d.name.startswith(p + "-")), None)
        if (provider_job_matches(d, url, provider) if provider else job_matches(d, url)):
            matched.append(((d / "notes.md").stat().st_mtime, d))
    if not matched:
        return None
    matched.sort(key=lambda t: -t[0])
    return matched[0][1]


def render(job: Path) -> str:
    try:
        p = subprocess.run([sys.executable, str(RENDER), str(job)], capture_output=True, text=True, timeout=30)
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def usable_draft(draft: str) -> bool:
    return bool(draft and not re.match(r"^\s*\(?render failed\b", draft, re.I))


def level1_pipeline(url: str) -> Path | None:
    if not video_identity(url) or not LINK.exists():
        return None
    try:
        subprocess.run([str(LINK), url], capture_output=True, text=True, timeout=540, cwd="/workspace/video-timeline-pipeline")
    except Exception as e:
        (LOG / "last-run.log").write_text(f"level1 error: {e}")
        return None
    return find_job(url)



def _brightdata_url(url: str) -> bool:
    platform = platform_for_url(url)
    return platform in {"instagram", "x", "threads"} and bool(content_id(url, platform))


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
    if p.returncode == 0 and isinstance(meta, dict) and meta.get("ok") and isinstance(meta.get("job"), str):
        job = Path(meta["job"])
        if provider_job_matches(job, url, "brightdata"):
            return job
    return None


def level2_apify(url: str) -> Path | None:
    if platform_for_url(url) != "instagram" or not content_id(url, "instagram"):
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
    if p.returncode == 0 and isinstance(meta, dict) and meta.get("ok") and isinstance(meta.get("job"), str):
        job = Path(meta["job"])
        if provider_job_matches(job, url, "apify"):
            return job
    return None


def level3_og(url: str) -> str:
    """Return a draft string from OG, or empty."""
    if platform_for_url(url) not in {"instagram", "facebook"}:
        return ""
    if not OG.exists():
        return ""
    try:
        p = subprocess.run([sys.executable, str(OG), url], capture_output=True, text=True, timeout=40)
        if p.returncode != 0:
            return ""
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
        if usable_draft(draft):
            out.update(ok=True, level=1, job=str(job), draft=draft, note="video-pipeline")
            return out

    # L2 Bright Data IG/X/Threads (preferred over Apify)
    job = level2_brightdata(url)
    if job and (job / "notes.md").exists():
        draft = render(job)
        if usable_draft(draft):
            # Infer platform note from job provider if present
            note = "brightdata-social"
            try:
                info = json.loads((job / "source.info.json").read_text())
                note = info.get("provider") or info.get("platform") or note
                if note in ("instagram", "x", "threads"):
                    note = f"brightdata-{note if note != 'instagram' else 'ig'}"
            except Exception:
                platform = platform_for_url(url)
                if platform == "instagram":
                    note = "brightdata-ig"
                elif platform == "threads":
                    note = "brightdata-threads"
                elif platform == "x":
                    note = "brightdata-x"
            out.update(ok=True, level=2, job=str(job), draft=draft, note=note)
            return out

    # L3 Apify IG (spare)
    job = level2_apify(url)
    if job and (job / "notes.md").exists():
        draft = render(job)
        if usable_draft(draft):
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
