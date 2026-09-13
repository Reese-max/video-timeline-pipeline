#!/usr/bin/env python3
"""Render a 可帶走 draft from video-pipeline job artifacts (argument-compressed)."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

QUOTES = ("「", "」", "『", "』", "《", "》", '"', "“", "”", "'", "‘", "’")


def _section(md: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*\n(.*?)(?=\n## |\Z)", md, re.S | re.M)
    return (m.group(1).strip() if m else "")


def _bullets(block: str, n=12):
    lines = []
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("- "):
            lines.append(s[2:].strip())
        elif re.match(r"^\d+\.", s):
            lines.append(re.sub(r"^\d+\.\s*", "", s))
    return lines[:n]


def _clean(s: str) -> str:
    s = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", s or "")
    s = re.sub(r"\*\*", "", s)
    s = re.sub(r"`[^`]*`", "", s)
    for q in QUOTES:
        s = s.replace(q, "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _clean_title(title: str) -> str:
    t = _clean(title)
    # strip FB engagement prefixes: "73K views · 8.7K reactions | ..."
    t = re.sub(
        r"^[\d.]+\s*[KMB]?\s*views?\s*[·•|].*?[|｜]\s*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(
        r"^[\d.]+\s*[KMB]?\s*reactions?\s*[·•|].*?[|｜]\s*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\s*[|｜]\s*[^|｜]{0,40}$", "", t)  # trailing "| Author"
    return t.strip(" .|-")[:100]


def _claim_from_chapter(c: str) -> str:
    c = _clean(c)
    # drop timecodes
    c = re.sub(r"`?0*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[–\-〜~]\s*`?0*\d{1,2}:\d{2}(?::\d{2})?)?`?", "", c)
    c = re.sub(r"^\s*[-–—]\s*", "", c)
    # "標題：說明" or "**標題**：說明"
    if "：" in c or ":" in c:
        parts = re.split(r"[：:]", c, 1)
        head, tail = parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""
        if len(head) <= 40 and tail:
            # prefer short head as claim label + clipped tail
            clip = tail[:70] + ("…" if len(tail) > 70 else "")
            return f"{head} — {clip}" if head else clip
    if len(c) > 90:
        c = c[:90] + "…"
    return c


def _one_liner(summary: str, title: str) -> str:
    s = _clean(summary)
    if not s:
        return (_clean_title(title) or "這支在講一套方法") + "～"
    # first sentence
    one = re.split(r"[。！？\n]", s)[0].strip()
    if len(one) > 120:
        one = one[:120] + "…"
    if one and not one.endswith(("～", "喔", "啦", "嘛", "耶", "呢")):
        one += "～"
    return one


def _fit_notes(topics, summary, title, steps) -> list[str]:
    """Build takeaway bullets including fit / not-fit."""
    out = []
    for t in topics[:4]:
        t2 = _clean(t)
        if t2:
            out.append(t2)
    blob = " ".join([summary, title] + steps + topics)
    # generic fit/not-fit if not already present
    has_fit = any("適合" in x or "不適合" in x or "別誤會" in x for x in out)
    if not has_fit:
        if any(k in blob for k in ("投資", "財富", "副業", "現金流", "Trading", "股票", "財務", "複利")):
            out.append("適合想理財入門聽框架的人；別誤會成保證致富或投資建議")
        elif any(k in blob for k in ("drill", "habit", "手寫", "寫作練習", "觀察練習")) or (
            any(k in blob for k in ("練習", "每天")) and "寫" in blob
        ):
            out.append("適合想建立固定練習／流程的人；不適合只想聽雞湯、不打算動手的")
        elif any(k in blob for k in ("產品", "App", "SaaS", "工具")):
            out.append("適合想快速搞懂這工具／方法的人；別把廣告話術當完整評測")
        else:
            out.append("適合想抓核心主張的人；別把摘要當成逐字稿或保證有效")
    if not any("別誤會" in x or "不是保證" in x or "不是" in x for x in out):
        out.append("別把短影音當完整結論；有需要再對照原文／脈絡喔")
    return out[:6]


def render(job: Path) -> str:
    notes = (job / "notes.md").read_text(errors="ignore") if (job / "notes.md").exists() else ""
    # prefer knowledge.md chapters if richer? stick to notes
    info = {}
    if (job / "source.info.json").exists():
        try:
            info = json.loads((job / "source.info.json").read_text())
        except Exception:
            info = {}
    # request url from source.json if present
    req_url = ""
    if (job / "source.json").exists():
        try:
            sj = json.loads((job / "source.json").read_text())
            req_url = (sj.get("url") or sj.get("input_url") or "") if isinstance(sj, dict) else ""
            if not req_url and isinstance(sj, dict) and isinstance(sj.get("source"), dict):
                req_url = sj["source"].get("url") or ""
        except Exception:
            pass

    raw_title = info.get("title") or (notes.splitlines()[0].lstrip("# ").strip() if notes else "")
    title = _clean_title(raw_title)
    uploader = info.get("uploader") or info.get("channel") or ""
    # strip "Connected Page" noise
    uploader = re.sub(r"\s*Connected Page\s*", "", uploader).strip()
    dur = info.get("duration_string") or ""
    if not dur and info.get("duration"):
        try:
            sec = float(info["duration"])
            dur = f"{int(sec // 60)}:{int(sec % 60):02d}"
        except Exception:
            dur = ""
    views = info.get("view_count")
    url = req_url or info.get("webpage_url") or ""
    summary = _section(notes, "摘要") or ""
    steps = [_clean(s) for s in _bullets(_section(notes, "操作步驟"), 10) if _clean(s)]
    chapters = _bullets(_section(notes, "章節"), 8)
    topics = _bullets(_section(notes, "主題重點"), 8)

    one = _one_liner(summary, title)

    lines = []
    lines.append("哼～這支我看完了啦")
    blob = title + summary + raw_title
    if any(k in blob for k in ("買", "訂閱", "免費社群", "START", "廣告", "Trading", "報名", "業配")):
        lines.append("類型：偏招生／推廣＋教學")
    elif any(k in blob for k in ("梗", "氛圍", "meme", "It can happen", "紅太陽", "hashtag")) and (
        "秒" in blob or "短" in blob
    ):
        lines.append("類型：短梗／氛圍片")
    else:
        lines.append("類型：教學片")
    lines.append("")
    lines.append("一句話")
    lines.append(one)
    lines.append("")
    meta_bits = [x for x in [uploader, dur, (f"{views} 次觀看" if views else "")] if x]
    lines.append("影片資訊")
    if title:
        lines.append(f"標題：{title}")
    if meta_bits:
        lines.append("・".join(meta_bits))
    if url:
        lines.append(url)
    lines.append("")

    # 他在講什麼：短主張，不要時間碼搬家
    claims = []
    for c in chapters[:6]:
        cl = _claim_from_chapter(c)
        if cl and cl not in claims:
            claims.append(cl)
    if not claims and summary:
        # split summary into up to 3 short claims
        parts = [p.strip() for p in re.split(r"[。！？]", _clean(summary)) if p.strip()]
        claims = [p if len(p) <= 80 else p[:80] + "…" for p in parts[:3]]
    if claims:
        lines.append("他在講什麼")
        for cl in claims[:5]:
            lines.append(f"- {cl}")
        lines.append("")

    if steps:
        lines.append("流程／步驟")
        for i, s in enumerate(steps[:7], 1):
            if len(s) > 100:
                s = s[:100] + "…"
            lines.append(f"{i}. {s}")
        lines.append("")
    elif claims and len(claims) >= 3:
        # derive light flow from claim heads
        lines.append("流程／步驟")
        for i, cl in enumerate(claims[:5], 1):
            head = cl.split(" — ")[0].strip()
            lines.append(f"{i}. {head}")
        lines.append("")

    blob_cta = notes + raw_title + summary
    # START must be whole-token uppercase (avoid "I'd start with...")
    hard_cta = bool(re.search(r"(?<![A-Za-z])START(?![A-Za-z])", blob_cta)) or bool(re.search(
        r"業配|Trading\s*212|限時|報名連結|免費社群|私訊\s*\w+|訂閱並|導流關鍵字",
        blob_cta,
        re.I,
    ))
    if hard_cta:
        lines.append("片尾在賣什麼")
        # one concrete line if we can sniff product
        m = re.search(r"(Trading\s*212|私訊\s*\w+|訂閱|免費社群|課程)", notes + summary, re.I)
        if m:
            lines.append(f"有導流：{m.group(1)} — 先當教學看，別被結尾帶跑～")
        else:
            lines.append("有導流／招生味道，重點先當教學看，別被結尾帶跑～")
        lines.append("")

    lines.append("可以帶走的重點")
    for t in _fit_notes(topics, summary, title, steps):
        lines.append(f"- {t}")

    out = "\n".join(lines).strip() + "\n"
    for q in QUOTES:
        out = out.replace(q, "")
    return out


if __name__ == "__main__":
    print(render(Path(sys.argv[1])))
