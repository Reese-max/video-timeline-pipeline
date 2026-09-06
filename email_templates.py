"""Email templates for video digests and pipeline notifications.

Generates dual-format (text/plain and text/html) payloads.
Guarantees XSS protection via html.escape and rejects hostile file:// / localhost URLs.
"""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlparse


def sanitize_link(url: str | None) -> str:
    """Return URL only if it uses safe HTTP/HTTPS schemes; otherwise empty string."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc.lower() not in ("localhost", "127.0.0.1", "0.0.0.0"):
        return url.strip()
    return ""


def render_digest_email(payload: dict[str, Any]) -> tuple[str, str]:
    """Render daily or weekly digest into (text_body, html_body)."""
    digest_type = payload.get("digest_type", "daily")
    type_name = "每日" if digest_type == "daily" else "每週"
    window_label = payload.get("window_label", "")
    items = payload.get("selected_items", [])
    deferred_count = payload.get("deferred_count", 0)
    candidate_count = payload.get("candidate_count", len(items))

    # --- 1. Plain Text ---
    text_lines = [
        f"【影片知識庫】{type_name}精選摘要",
        f"統計區間：{window_label}",
        f"候選影片：{candidate_count} 支｜精選涵蓋：{len(items)} 支" + (f"｜稍後遞延：{deferred_count} 支" if deferred_count > 0 else ""),
        "-" * 50,
        "",
    ]

    if not items:
        text_lines.append("本週期尚無已完成的精選影片。")
    else:
        for idx, item in enumerate(items, 1):
            title = item.get("title", "未命名影片")
            url = sanitize_link(item.get("url")) or "無公開連結"
            minutes = item.get("minutes", 0)
            summary = item.get("summary", "").strip()
            chapters = item.get("chapters", [])
            terms = item.get("terms", [])

            text_lines.append(f"{idx}. {title}（{minutes} 分鐘）")
            text_lines.append(f"   連結：{url}")
            if summary:
                text_lines.append(f"   摘要：{summary}")
            if terms:
                text_lines.append(f"   關鍵字：{'、'.join(terms[:8])}")
            if chapters:
                text_lines.append("   精華章節：")
                for ch in chapters[:3]:
                    ch_time = ch.get("timestamp", "")
                    ch_title = ch.get("title", "")
                    text_lines.append(f"     · [{ch_time}] {ch_title}")
            text_lines.append("")

    if deferred_count > 0:
        text_lines.append(f"註：另有 {deferred_count} 支影片已自動保留至下一期摘要。")
    text_lines.append("-" * 50)
    text_lines.append("由 Video Timeline Pipeline 自動產生")
    text_body = "\n".join(text_lines)

    # --- 2. HTML Body ---
    html_items = []
    for idx, item in enumerate(items, 1):
        safe_title = html.escape(item.get("title", "未命名影片"))
        raw_url = sanitize_link(item.get("url"))
        safe_url = html.escape(raw_url)
        minutes = item.get("minutes", 0)
        safe_summary = html.escape(item.get("summary", "").strip())
        chapters = item.get("chapters", [])
        terms = item.get("terms", [])

        link_markup = (
            f'<a href="{safe_url}" style="color:#2563eb;text-decoration:none;font-weight:bold;">{safe_title}</a>'
            if safe_url
            else f'<span style="font-weight:bold;color:#1e293b;">{safe_title}</span>'
        )

        terms_markup = ""
        if terms:
            terms_tags = "".join(
                f'<span style="display:inline-block;background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:4px;font-size:12px;margin-right:6px;margin-bottom:4px;">{html.escape(t)}</span>'
                for t in terms[:8]
            )
            terms_markup = f'<div style="margin-top:6px;">{terms_tags}</div>'

        chapters_markup = ""
        if chapters:
            ch_list = "".join(
                f'<li style="margin-bottom:3px;"><span style="color:#64748b;font-family:monospace;font-size:12px;">[{html.escape(str(ch.get("timestamp", "")))}]</span> {html.escape(str(ch.get("title", "")))}</li>'
                for ch in chapters[:3]
            )
            chapters_markup = f'<ul style="margin:8px 0 0 18px;padding:0;color:#475569;font-size:13px;">{ch_list}</ul>'

        card = f"""
        <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.05);">
          <div style="font-size:16px;line-height:1.4;margin-bottom:4px;">
            <span style="color:#94a3b8;font-size:14px;margin-right:6px;">#{idx}</span>
            {link_markup}
            <span style="color:#64748b;font-size:12px;margin-left:6px;">· {minutes} 分鐘</span>
          </div>
          {f'<p style="margin:8px 0;color:#334155;font-size:14px;line-height:1.6;">{safe_summary}</p>' if safe_summary else ''}
          {chapters_markup}
          {terms_markup}
        </div>
        """
        html_items.append(card)

    empty_markup = '<div style="background:#f8fafc;padding:24px;text-align:center;color:#64748b;border-radius:8px;">本週期尚無已完成的精選影片。</div>'
    items_content = "\n".join(html_items) if html_items else empty_markup

    deferred_notice = ""
    if deferred_count > 0:
        deferred_notice = f'<p style="color:#64748b;font-size:13px;text-align:center;margin-top:16px;">註：另有 {deferred_count} 支影片已自動保留至下一期摘要。</p>'

    html_body = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>【影片知識庫】{type_name}精選摘要</title>
</head>
<body style="margin:0;padding:20px;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #cbd5e1;">
    <div style="background:#1e293b;color:#ffffff;padding:24px 28px;">
      <h1 style="margin:0;font-size:20px;letter-spacing:0.5px;">【影片知識庫】{type_name}精選摘要</h1>
      <div style="margin-top:6px;font-size:13px;color:#94a3b8;">
        區間：{html.escape(window_label)} · 涵蓋 {len(items)} / {candidate_count} 支影片
      </div>
    </div>
    <div style="padding:24px 28px;background:#f8fafc;">
      {items_content}
      {deferred_notice}
    </div>
    <div style="background:#f1f5f9;border-top:1px solid #e2e8f0;padding:16px 28px;text-align:center;font-size:12px;color:#64748b;">
      由 Video Timeline Pipeline 自動產生 · 零外部模型計費
    </div>
  </div>
</body>
</html>"""

    return text_body, html_body


def render_event_email(event_type: str, item: dict[str, Any]) -> tuple[str, str]:
    """Render a single event notification (e.g., video.completed, pipeline.error)."""
    title = item.get("title", "未命名事件")
    raw_url = sanitize_link(item.get("url") or item.get("source_url"))
    safe_title = html.escape(title)
    safe_url = html.escape(raw_url)
    error_msg = item.get("error", "")

    if event_type == "video.completed":
        subject = f"【影片完成】{title}"
        text = f"影片已處理完成：\n標題：{title}\n連結：{raw_url or '無'}\n時間軸與摘要已存入本地知識庫。"
        html_msg = f"""
        <div style="font-family:sans-serif;padding:20px;max-width:600px;border:1px solid #e2e8f0;border-radius:8px;">
          <h2 style="color:#0f172a;margin-top:0;">{subject}</h2>
          <p>影片已順利分析並產出時間軸摘要：</p>
          <p><a href="{safe_url}" style="color:#2563eb;font-weight:bold;">{safe_title}</a></p>
        </div>"""
    else:
        subject = f"【系統通知】{event_type} - {title}"
        text = f"事件：{event_type}\n標題：{title}\n詳情：{error_msg or '無'}"
        html_msg = f"""
        <div style="font-family:sans-serif;padding:20px;max-width:600px;border:1px solid #e2e8f0;border-radius:8px;">
          <h2 style="color:#dc2626;margin-top:0;">{html.escape(subject)}</h2>
          <p><strong>事件：</strong>{html.escape(event_type)}</p>
          <p><strong>詳情：</strong>{html.escape(str(error_msg))}</p>
        </div>"""

    return text, html_msg
