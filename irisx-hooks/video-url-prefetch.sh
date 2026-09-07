#!/usr/bin/env bash
# pre_llm_call: video URL hard-intercept via takeaway cascade
set -euo pipefail
payload=$(cat)
export IRISX_HOOK_PAYLOAD="$payload"
python3 - <<'PY'
import json, os, re, subprocess, sys
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
            return json.loads(bundle_path.read_text())
        except Exception:
            return {}
    return {}


def save_bundle(b):
    bundle_path.write_text(json.dumps(b, ensure_ascii=False, indent=2))
    (logdir / f"{sid}.url").write_text(b.get("url") or "")


def render_draft(job: Path) -> str:
    try:
        p = subprocess.run([sys.executable, str(RENDER), str(job)], capture_output=True, text=True, timeout=30)
        return (p.stdout or "").strip()
    except Exception as e:
        return f"(render failed: {e})"


def build_context(url, draft, *, level=0, follow=False, note=""):
    parts = [
        "【硬攔截／影音連結】禁止回覆抓不到、要登入、貼文字、貼截圖（除非級聯全失敗）。",
        "【硬規則】必須用可帶走骨架講「這一支」URL。禁止舊影片充數。禁止主動推行動表／打卡。",
        "【寫法】幾乎照【可帶走草稿】改呆呆口吻（～／啦嘛喔、第一人稱、不要「」）。",
        f"【級聯】level={level} note={note}",
    ]
    if follow:
        parts.append("【追問】問上一支 session 快取；沒有新 URL。")
    if url:
        parts.append(f"URL: {url}")
    if draft:
        parts.append("【可帶走草稿 — 幾乎照抄改語氣】\n" + draft)
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

    result = {"ok": False, "level": 0, "draft": "", "job": "", "note": "cascade_missing"}
    try:
        p = subprocess.run(
            [sys.executable, str(CASCADE), url],
            capture_output=True, text=True, timeout=900,
        )
        (logdir / "last-cascade.json").write_text(((p.stdout or "") + "\n" + (p.stderr or ""))[-200000:])
        line = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else "{}"
        result = json.loads(line)
    except Exception as e:
        result = {"ok": False, "level": 4, "draft": f"哼～級聯出錯：{e}\n", "job": "", "note": "cascade_error", "keys": []}

    draft = result.get("draft") or ""
    job = result.get("job") or ""
    b = {
        "url": url,
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
        "context": build_context(url, draft, level=result.get("level") or 0, note=result.get("note") or "")
    }, ensure_ascii=False))
    sys.exit(0)

# Path B: follow-up
if FOLLOW_RE.search(umsg):
    b = load_bundle()
    if b.get("draft") and b.get("url"):
        (sess_dir / f"{sid}.pending").write_text("1")
        print(json.dumps({
            "context": build_context(b.get("url") or "", b.get("draft") or "", level=b.get("level") or 0, follow=True, note="session-cache")
        }, ensure_ascii=False))
        sys.exit(0)
    if b.get("job"):
        jp = Path(b["job"])
        if jp.exists() and (jp / "notes.md").exists():
            draft = render_draft(jp)
            b["draft"] = draft
            save_bundle(b)
            (sess_dir / f"{sid}.pending").write_text("1")
            print(json.dumps({
                "context": build_context(b.get("url") or "", draft, follow=True, note="session-rerender")
            }, ensure_ascii=False))
            sys.exit(0)

sys.exit(0)
PY
