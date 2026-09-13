---
name: video-takeaway-cascade
description: >-
  use this when pasting FB/IG/YT/TikTok/X/Threads links that need a reliable 可帶走 video
  summary cascade
---
# Video takeaway cascade (IRISX)

Use when the user pastes a social video/link (Facebook / Instagram / YouTube / TikTok / X / Threads / reel / share) and needs a reliable 可帶走 summary.

## Cascade (in order)

1. **L1** `link-summarize.sh` / `video-pipeline` (yt-dlp + transcript → `notes.md`)
2. **L2** Bright Data social via `brightdata_ig_fallback.py` (MCP HTTP; `BRIGHTDATA_API_TOKEN` / `BRIGHTDATA_MCP_TOKEN`) — **preferred**
   - Instagram reel → `web_data_instagram_reels` (+ Datasets REST `gd_lyclm20il4r5helnj` IG-only fallback)
   - X / Twitter (`x.com`, `twitter.com`) → `web_data_x_posts`
   - Threads (`threads.net`) → `scrape_as_markdown` (HTML scrape if markdown thin); no dedicated extractor
3. **L3** Instagram only: Apify (`apify_ig_fallback.py`) using `APIFY_TOKEN` — spare when Bright Data fails
4. **L4** Clean OG/embed preview (`ig_og_fallback.py`) — IG/FB; reject mojibake; mark as preview-only
5. **L5** Honest failure — never invent content, never reuse another video’s job

## Hard rules

- Match jobs only by distinctive IDs (`/share/v/`, `/reel/`, `/status/`, Threads `/post/`, `watch?v=`). Never match on bare `facebook` / `mibextid` / `status` / `twitter` / `threads`.
- On a **new URL**, clear the previous session draft before running.
- Render with `render_takeaway.py` (compressed claims; omit 片尾 unless hard CTA).
- Reply skeleton: type → one-liner → short meta → claims/flow → takeaways (fit/not-fit). Ban unsolicited 行動表.
- Prefer Bright Data over Apify to conserve Apify free credits.
- Threads path is best-effort (login wall / thin markdown possible); report honestly if unusable.

## Entry points

```bash
python3 /home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py 'URL'
```

Hermes `pre_llm_call` hook: `video-url-prefetch.sh` (calls the cascade; VIDEO_HINT includes `threads.net|x.com|twitter.com|t.co`).

## After success

Read `jobs/<id>/notes.md` + `source.info.json`, inject the rendered draft, let the persona rewrite tone only.
