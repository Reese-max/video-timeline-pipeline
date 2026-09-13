# IRISX LINE video takeaway hooks

Hermes / LINE（呆呆）用的影片連結「可帶走」cascade。部署時把本目錄複製到 Hermes profile 的 `hooks/`（或軟連），並確保 `video-url-prefetch.sh` 會呼叫 `video_takeaway_cascade.py`。

## Cascade

1. **L1** `video-timeline-pipeline`（yt-dlp 等本機管線）
2. **L2** Bright Data social（優先）— IG Reels / X posts / Threads markdown  
   - MCP HTTP：`web_data_instagram_reels`、`web_data_x_posts`、`scrape_as_markdown`  
   - IG 另有 Datasets REST fallback（`gd_lyclm20il4r5helnj`）
3. **L3** Apify Instagram reel scraper（spare）
4. **L4** OG / 頁面預覽
5. **L5** 誠實失敗（不灌假 takeaway）

## Env（勿 commit 真值）

| 變數 | 用途 |
|------|------|
| `BRIGHTDATA_MCP_TOKEN` | Bright Data MCP token（優先） |
| `BRIGHTDATA_API_TOKEN` | Datasets REST / 與 MCP 共用亦可 |
| `APIFY_TOKEN` / `APIFY_API_TOKEN` | L3 spare |

也可放在 box `box-secrets.json` 同名鍵，或 pipeline `.env`（已在 `.gitignore`）。

## Files

| File | Role |
|------|------|
| `video-url-prefetch.sh` | Prefetch hook；偵測 FB/IG/YT/TikTok/X/Threads |
| `video_takeaway_cascade.py` | L1–L5 入口 |
| `brightdata_ig_fallback.py` | L2 Bright Data（檔名歷史因素仍含 `ig`） |
| `apify_ig_fallback.py` | L3 Apify |
| `ig_og_fallback.py` | L4 OG |
| `render_takeaway.py` | 可帶走草稿渲染 |

## Known limits

- Bright Data MCP / X 偶發 timeout，重試通常可過
- Threads 無專用 extractor；常為 caption 級、可能 login-walled
- IG datacenter IP 可能擋 yt-dlp；依賴 L2/L3

詳見 `docs/video-takeaway-cascade.md`。
