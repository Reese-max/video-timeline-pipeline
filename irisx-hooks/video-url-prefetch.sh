#!/usr/bin/env bash
# pre_llm_call: video URL hard-intercept via takeaway cascade
set -euo pipefail
payload=$(cat)
export IRISX_HOOK_PAYLOAD="$payload"
python3 - <<'PY'
import hashlib, importlib.util, json, os, re, subprocess, sys
from pathlib import Path

raw = os.environ.get("IRISX_HOOK_PAYLOAD") or ""
try:
    o = json.loads(raw)
except Exception:
    sys.exit(0)
extra = o.get("extra") if isinstance(o.get("extra"), dict) else {}
umsg = extra.get("user_message") or o.get("user_message") or extra.get("message") or ""
if not isinstance(umsg, str):
    umsg = str(umsg or "")
sid = str(o.get("session_id") or extra.get("session_id") or "") or "nosid"
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", sid):
    sid = "sid~" + hashlib.sha256(sid.encode("utf-8", errors="surrogatepass")).hexdigest()

URL_RE = re.compile(r"https?://[^\s<>\"\']+", re.I)
VIDEO_HINT = re.compile(
    r"facebook\.com|fb\.watch|fb\.gg|instagram\.com|youtu\.be|youtube\.com|"
    r"tiktok\.com|vm\.tiktok\.com|bilibili\.com|douyin\.com|/reel/|/reels/|"
    r"/share/v|/share/r/|vimeo\.com|web\.facebook\.com|"
    r"threads\.net|x\.com|twitter\.com|t\.co",
    re.I,
)
FOLLOW_RE = re.compile(
    r"(這部|這支|該|那部|那支).{0,6}(影片|視頻|片|內容)|告訴我.{0,8}(內容|重點|摘要)|"
    r"(影片|視頻).{0,4}(內容|重點|摘要|在講|說什麼)|再講一次|完整一點|可帶走|"
    r"重點是什麼|講什麼|在賣什麼|流程|步驟",
    re.I,
)

logdir = Path("/home/box/irisx-failover-restore/20260906/runtime/video-hook")
logdir.mkdir(parents=True, exist_ok=True)
sess_dir = logdir / "sessions"
sess_dir.mkdir(parents=True, exist_ok=True)
bundle_path = sess_dir / f"{sid}.json"
CASCADE = Path("/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py")
RENDER = Path("/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/render_takeaway.py")

urls = [u.rstrip(").,，。]") for u in URL_RE.findall(umsg)]
urls = [u for u in urls if VIDEO_HINT.search(u)]
(logdir / "last-input.json").write_text(
    json.dumps({"umsg": umsg[:500], "urls": urls, "sid": sid, "follow": bool(FOLLOW_RE.search(umsg))}, ensure_ascii=False)
)


def load_bundle():
    if bundle_path.exists():
        try:
            value = json.loads(bundle_path.read_text())
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def save_bundle(b):
    bundle_path.write_text(json.dumps(b, ensure_ascii=False, indent=2))
    (logdir / f"{sid}.url").write_text(b.get("url") or "")


def usable_draft(draft):
    return isinstance(draft, str) and bool(draft.strip()) and not re.match(r"^\s*\(?render failed\b", draft, re.I)


def trusted_job(job_value, url):
    if not isinstance(job_value, str) or not job_value or not url:
        return False
    try:
        sys.path.insert(0, str(CASCADE.parent))
        spec = importlib.util.spec_from_file_location("_irisx_cascade_validation", CASCADE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job = Path(job_value).resolve(strict=True)
        job.relative_to(module.JOBS.resolve(strict=True))
        if not (job / "notes.md").is_file():
            return False
        for provider in ("brightdata", "apify"):
            if job.name.startswith(provider + "-"):
                return module.provider_job_matches(job, url, provider)
        return module.job_matches(job, url)
    except Exception:
        return False


def render_draft(job: Path) -> str:
    try:
        p = subprocess.run([sys.executable, str(RENDER), str(job)], capture_output=True, text=True, timeout=30)
        draft = (p.stdout or "").strip() if p.returncode == 0 else ""
        return draft if usable_draft(draft) else ""
    except Exception:
        return ""


def build_context(url, draft, *, level=0, follow=False, note="", failed=False):
    if failed:
        parts = ["【級聯失敗】不得聲稱已取得影片內容或使用其他影片充數。"]
    else:
        parts = [
            "【硬攔截／影音連結】禁止回覆抓不到、要登入、貼文字、貼截圖（除非級聯全失敗）。",
            "【硬規則】必須用可帶走骨架講「這一支」URL。禁止舊影片充數。禁止主動推行動表／打卡。",
            "【寫法】幾乎照【可帶走草稿】改呆呆口吻（～／啦嘛喔、第一人稱、不要「」）。",
        ]
    parts.append(f"【級聯】level={level} note={note}")
    if follow:
        parts.append("【追問】問上一支 session 快取；沒有新 URL。")
    if url:
        parts.append(f"URL: {url}")
    if draft:
        parts.append(("【級聯失敗回報】\n" if failed else "【可帶走草稿 — 幾乎照抄改語氣】\n") + draft)
    else:
        parts.append("級聯未產出草稿；不可捏造內容。")
    return "\n\n".join(parts)


# Path A: new URL → full cascade
if urls:
    url = urls[0]
    old = load_bundle()
    if old.get("url") and old.get("url") != url:
        save_bundle({"url": url, "job": "", "draft": "", "updated": False, "prev_url": old.get("url")})
        pending = sess_dir / f"{sid}.pending"
        if pending.exists():
            pending.unlink()

    result = {}
    try:
        p = subprocess.run(
            [sys.executable, str(CASCADE), url],
            capture_output=True, text=True, timeout=900,
        )
        (logdir / "last-cascade.json").write_text(((p.stdout or "") + "\n" + (p.stderr or ""))[-200000:])
        line = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else "{}"
        result = json.loads(line) if p.returncode == 0 else {}
    except Exception:
        result = {}

    if not isinstance(result, dict) or result.get("url") != url:
        result = {}
    level = result.get("level")
    success = result.get("ok") is True and type(level) is int and usable_draft(result.get("draft")) and (
        (level in {1, 2, 3} and trusted_job(result.get("job"), url))
        or (level == 4 and result.get("note") == "og-preview" and not result.get("job"))
    )
    if not success and not (result.get("ok") is False and result.get("note") == "all_levels_failed" and usable_draft(result.get("draft"))):
        result = {"ok": False, "level": 5, "draft": "目前無法確認這支影片的內容，不能以其他影片代替。", "job": "", "note": "unverified_result", "keys": []}

    draft = result.get("draft") or ""
    job = result.get("job") if success and isinstance(result.get("job"), str) else ""
    b = {
        "url": url,
        "verified_url": url,
        "session_schema": 2,
        "outcome": "success" if success else "failure",
        "job": job,
        "draft": draft,
        "keys": result.get("keys") or [],
        "level": result.get("level"),
        "note": result.get("note"),
        "updated": bool(draft),
    }
    save_bundle(b)
    (sess_dir / f"{sid}.pending").write_text("1")
    print(json.dumps({
        "context": build_context(url, draft, level=result.get("level") or 0, note=result.get("note") or "", failed=not success)
    }, ensure_ascii=False))
    sys.exit(0)

# Path B: follow-up
if FOLLOW_RE.search(umsg):
    b = load_bundle()
    if b.get("session_schema") != 2 or not b.get("url") or b.get("verified_url") != b.get("url") or b.get("outcome") not in {"success", "failure"}:
        sys.exit(0)
    if usable_draft(b.get("draft")) and b.get("url"):
        (sess_dir / f"{sid}.pending").write_text("1")
        print(json.dumps({
            "context": build_context(b.get("url") or "", b.get("draft") or "", level=b.get("level") or 0, follow=True, note="session-cache", failed=b.get("outcome") == "failure")
        }, ensure_ascii=False))
        sys.exit(0)
    if b.get("outcome") == "success" and trusted_job(b.get("job"), b.get("url")):
        jp = Path(b["job"])
        if jp.exists() and (jp / "notes.md").exists():
            draft = render_draft(jp)
            if not draft:
                sys.exit(0)
            b["draft"] = draft
            save_bundle(b)
            (sess_dir / f"{sid}.pending").write_text("1")
            print(json.dumps({
                "context": build_context(b.get("url") or "", draft, follow=True, note="session-rerender")
            }, ensure_ascii=False))
            sys.exit(0)

sys.exit(0)
PY
