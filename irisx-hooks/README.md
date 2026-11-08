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
| `irisx_identity.py` | 社群主機、貼文 ID 與來源記錄的比對 |
| `brightdata_ig_fallback.py` | L2 Bright Data（檔名歷史因素仍含 `ig`） |
| `apify_ig_fallback.py` | L3 Apify |
| `ig_og_fallback.py` | L4 OG |
| `render_takeaway.py` | 可帶走草稿渲染 |

## Known limits

- Bright Data MCP / X 偶發 timeout，重試通常可過
- Threads 無專用 extractor；常為 caption 級、可能 login-walled
- 工作只能以來源 metadata 的實際 URL／ID 比對，筆記本文與自行寫入的 request URL 不作為來源證據；缺少身分或來源衝突時會回報失敗。
- Threads 的 markdown／HTML 若沒有相符的結構化來源 URL 或 HTML canonical link，會標記來源未驗證並停止使用；真實 Bright Data 回傳是否提供這些欄位仍需另行驗證。
- 無法從短網址辨識貼文 ID 時，付費 fallback 不會啟動；可改用有明確貼文 ID 的原始社群網址。
- IG datacenter IP 可能擋 yt-dlp；依賴 L2/L3

詳見 `docs/video-takeaway-cascade.md`。
