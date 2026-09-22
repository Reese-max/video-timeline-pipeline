# 影片資料庫時間軸管線

目前版本 `0.2.0`，採 [MIT License](LICENSE)；版本變更見 [CHANGELOG.md](CHANGELOG.md)。

目前實作的流程：

```text
VidBee 完成的本機影音（或影片 URL） → FFmpeg
                         ├─ 音訊片段 → Groq Whisper
                         └─ steipete slides／Tesseract → MiniMax M3
                                      ↓
                              時間軸合併 → MiniMax M3 摘要
```

## 專案邊界

目前刻意維持單一可執行入口；`pipeline.py` 內的函式依責任分成六段，避免為了分檔增加部署負擔：

```text
輸入／工作目錄
  ├─ yt-dlp：來源下載
  ├─ FFmpeg：ffprobe、音訊切段、固定間隔截圖
  ├─ native／steipete：畫面候選與 OCR
  ├─ Groq／MiniMax：逐字稿、視覺理解、摘要
  └─ transcript.srt／notes.md／knowledge.md：可回查輸出與 VidBee sidecar
```

每個工作目錄的雜湊只綁來源，讓同一支影片能在原工作目錄續跑；音訊、畫面與雲端產物各自用來源指紋、設定、模型及實際輸入判斷是否失效。JSON 與 Markdown 產物以暫存檔置換，避免中斷時留下半份索引。

根目錄中受版本控制的內容以入口、設定範例與既有知識庫快照為主；本機執行產物維持在固定位置，方便備份與清理：

| 類型 | 路徑 | Git |
|---|---|---|
| 程式與說明 | `pipeline.py`、`README.md`、`requirements.txt`、`.env.example` | 追蹤 |
| 既有知識庫快照 | `urls.txt`、`categories.json`、`digest.md` | 追蹤 |
| 每支影片與批次產物 | `jobs/`、`batches/` | 忽略 |
| RSS 狀態與驗證產物 | `rss/`、`canary/` | 忽略 |
| 本機索引與秘密 | `library.db`、`.env`、`.env.bak-*` | 忽略 |

`urls.txt`、`categories.json` 與 `digest.md` 已是版本庫的一部分，而且現有 CLI 會在根目錄讀寫這些預設檔名，因此不為了目錄外觀搬移。需要完全獨立的資料集時，使用 `--root <目錄>` 即可，不必複製程式。

多層快取讓中斷的批次可以直接續跑，不會重複付費：

| 產物 | 判準 |
|---|---|
| `source.probe.json` | 來源大小＋最後修改時間；未變就不重跑 `ffprobe` |
| `audio/`、`frames/`、`frame_candidates.json` | 來源指紋＋抽取設定；半份產物沒有 `.complete` 就清掉重做 |
| `transcription-cache/` | 音訊 SHA-256＋模型＋語言＋時間戳參數；失敗只重跑缺少的片段 |
| `vision-cache/` | 圖片 SHA-256＋時間戳＋OCR＋模型＋提示詞；失敗只重跑缺少的圖片 |
| `summary-cache/` | 提示詞版本＋模型＋實際輸入；超長內容逐段保存後再整合 |
| `library.db` 的 `index_state` | 時間軸、摘要與分類未變時，不再刪除並重建全文索引 |

Groq 轉錄與 MiniMax 畫面分析會同時執行；超長逐字稿的 MiniMax 分段摘要也會受限並行。`--workers` 預設／最多 4；畫面分析另由 `--vision-workers` 控制，預設／最多 8。畫面張數預設依片長每 30 秒約 1 張、最少 8、最多 40；`--max-vision-frames N` 可手動覆寫。抽樣點會保持穩定，增加張數時可沿用已分析畫面的快取。不會為了速度切換到較不精確的 Whisper turbo。純色／空白畫面會先由 Pillow 判定，避免視覺模型捏造內容；MiniMax-M3 摘要使用官方 `thinking: {"type":"disabled"}` 直接回答，畫面分析仍保留 adaptive thinking。`reasoning_split` 只改回傳格式，不能關閉推理。

2026-09-01 以同一支 8 秒公開合成影片、全新輸出目錄與真實 Groq／MiniMax API 實測：調整前兩次首跑為 17.003、23.049 秒；調整後為 14.352、18.732 秒，兩次平均約縮短 17%。Groq 轉錄僅 0.438–0.484 秒，剩餘波動主要在 MiniMax；這是小樣本觀察值，不是供應商延遲保證。兩次輸出皆有 2 段逐字稿、5 題問答、有效引用、`notes.md` 與 `transcript.srt`。

暖快取仍可避免重複付費：隔離的 10.2 秒本機樣本重跑由 1.383 秒降到 0.667 秒，不掛 profiler 為 0.380 秒。要強制重算雲端產物加 `--refresh`。

## 先做本地驗證

```powershell
cd 'C:\Users\Administrator\video-timeline-pipeline'
python -m unittest discover -s tests -v
python pipeline.py --self-test
python pipeline.py --video 'D:\path\to\video.mp4' --dry-run
python pipeline.py --video 'D:\path\to\video.mp4' --dry-run --slide-extractor steipete
```

`--dry-run` 不會下載、不會上傳、不會呼叫雲端 API，但會用本機 `ffprobe`／`ffmpeg` 產生音訊片段與候選截圖。輸出在 `jobs\<12字元雜湊>\`。

## ChatGPT 網頁 MCP 影片摘要

`mcp_server.py` 是最小的 ChatGPT Apps/MCP 入口：工具接受公開影片 URL，沿用本管線的 `yt-dlp` 下載，再用 FFmpeg 切成 MP4；ChatGPT widget 會把分段上傳到目前對話，取得暫存下載網址後呼叫 `ingest_video_segments`，確認 `file_id`／`download_url` 交接成功才送出摘要要求。影片分段與代表影格放在 Git 忽略的 `mcp_jobs\`。

先在隔離環境安裝依賴（FFmpeg／ffprobe 仍需在 PATH）：

```powershell
cd 'C:\Users\Administrator\video-timeline-pipeline'
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

啟動本機 MCP：

```powershell
$env:MCP_PUBLIC_BASE_URL = 'https://你的 HTTPS tunnel 網址'
$env:MCP_MEDIA_BASE_URL = 'https://可由 ChatGPT 存取的公開 HTTPS 網址'
$env:MCP_HOST = '127.0.0.1'
$env:MCP_PORT = '8787'
.\.venv\Scripts\python mcp_server.py
```

本機檢查：`Invoke-RestMethod http://127.0.0.1:8787/health`；ChatGPT Developer Mode 則新增 MCP app，填入 MCP tunnel 網址加 `/mcp`。若 MCP 入口是 Tailscale 或其他 ChatGPT 分析環境無法解析的網域，另設 `MCP_MEDIA_BASE_URL` 指向公開 HTTPS 媒體出口；工具完成後按 widget 的「上傳影片片段並摘要」。影片檔上傳、檔案引用交接與模型實際讀取是獨立驗收 gate。host 有 `uploadFile`／`getFileDownloadUrl` 時嘗試原生附件，不代表已驗證該 host 接受 MP4；若缺少 API，或任一分段下載／上傳失敗（包括臨時網址失效、CORS、`Failed to fetch`），widget 停止剩餘原生嘗試，將所有分段存到私人 Google Drive，顯示可複製的一般對話交接內容。Drive 失敗時停止並顯示錯誤，不會自動以伺服器端摘要代替 ChatGPT 讀取。`analyze_prepared_video` 仍保留為使用者明確選擇的獨立工具。更新後須重新整理 ChatGPT 外掛動作並產生新 widget；舊訊息不會自動變成 v9。

v9 修正 developer MCP 與官方 Drive 的交接邊界：不在原對話送出 Drive 讀取／摘要 follow-up；先開啟未啟用 developer MCP 的一般 ChatGPT 對話，選擇官方 Google Drive，再貼上 widget 的交接內容（job ID、全部檔案 ID／網址／大小與原片分段時間）。新對話須實際取得 bytes、記錄大小／SHA-256 並解碼，不能把 `drive_ready` 當成讀取成功。一般對話連結不會自動改變帳號的 developer mode 或連線設定，使用者需確認對話類型。登入、OAuth、Drive 檔案權限均未變更。UI URI 為 `ui://video-timeline-pipeline/v9-<媒體網址 SHA-256 前 12 碼>.html`，避免換網址後仍使用舊 CSP 快取。

媒體 tunnel worker 現在會驗證 DNS／TLS 後才發佈網址，每 30 秒檢查公開 `/health`；連續三次失敗才重建自己的 cloudflared 子行程。來源服務尚未啟動／故障或公開端 HTTP 429 不會觸發無效的 tunnel 重建。tunnel 不可用時，啟動器忽略舊網址並退回 MCP 原有公開 origin，保留 MCP／Drive 功能，不宣稱原生附件已恢復；現有 MCP 若仍持有不同的媒體網址，啟動器回傳非零，不強制中斷正在處理的影片。網址更換後，仍須受控重載 MCP、重新整理 ChatGPT 動作並 prepare 新 widget，才能更新確切的 CSP 網域；舊 widget 須使用 Drive 交接。回歸檢查（Linux／WSL）：`python3 -m unittest discover -s tests -p test_media_tunnel.py -v`。這不等於外部 ChatGPT 能下載 MP4 的驗收；[Quick Tunnels 官方不提供 SLA，僅供開發測試](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)，固定網域部署需另行設定 named tunnel。本次不新增網域或更動公開入口。

v7 修正跨工作狀態：切換 job 時立即替換 model context、清除舊附件 ID，並以工作快照及遞增版本隔離非同步結果（包含 A → B → A）。舊作業回傳或失敗不得覆寫目前 context、送出摘要要求或改寫目前錯誤；待舊呼叫結束後僅啟動最新工作。已送出的上傳可能在遠端完成，但過期結果會被丟棄，不刪除已建立的檔案。Drive 回應的 job_id 必須吻合；資料型交接回應不會當成新影片。回歸測試：`node tests/test_widget_handoff.cjs`。UI URI 按 [官方快取規則](https://developers.openai.com/plugins/build/chatgpt-ui#embed-the-component-in-the-server-response) 升版為 `ui://video-timeline-pipeline/v7.html`。

v8 保留 v7 的工作隔離，下載期限改為涵蓋 HTTP 回應與完整 `blob()` 讀取；內容串流卡住也會在 30 秒中止並走 Drive fallback。UI URI 為 `ui://video-timeline-pipeline/v8.html`。時間軸改讀 FFmpeg `segments.csv` 的實際 muxer 起訖（非 `index × segment_seconds`），並停用負時間戳自動平移，避免編碼延遲造成偏移；manifest 升為 v2，重新 prepare 時會重建舊估算快取。超過 999 的分段使用數字排序，媒體與預覽路由亦接受較長索引。

Drive 上傳以 1 MiB 分塊；遇到斷線、HTTP 429／5xx，最多退避重試三次，先查詢同一 resumable session 的已接收範圍，再續傳，避免盲目重建檔案。回歸檢查：`node tests/test_widget_handoff.cjs` 與 `python -m unittest discover -s tests -p test_mcp_server.py -v`。

跨呼叫重試也保留進度：採 [Google Drive 預先配置 ID](https://developers.google.com/workspace/drive/api/guides/manage-uploads#use_a_pre-generated_id_to_upload_files)，先原子保存 ID 再建立資料夾／影片。每次重用都重新讀取 Drive，確認未進垃圾桶、大小、SHA-256 與父資料夾；403／網路錯誤會停止，不把它當成不存在。已刪除或被修改的檔案會建立新 ID，不覆寫原檔。只有全部驗證成功才標示 `drive_ready`。舊版未記錄的孤立上傳不會自動刪除。此實作使用單程序鎖與 JSON checkpoint；多程序部署需先換成共享鎖，大量分段若 checkpoint 寫入成為瓶頸，再改成增量日誌。

可用環境變數：`MCP_MAX_SOURCE_MB`（預設 512）、`MCP_MAX_SEGMENT_SECONDS`（預設 600）、`MCP_MAX_SEGMENTS`（預設 0，不限段數；設為正整數後，省略 `max_segments` 也不會繞過伺服器上限）、`MCP_MIN_FREE_MB`（預設保留 1024 MiB）、`MCP_ROOT`（預設 repo 根目錄）、`TRANSCRIBE_BACKEND`（`groq` 或 `local`）、`GROQ_ASR_MODEL`、`MINIMAX_MODEL`。工具的 `max_segments` 預設仍不設上限。

容量保護是事前檢查：下載前預留來源上限，切割／分析前至少再預留一份來源大小與最低剩餘空間；轉碼大小不固定，這不是磁碟配額保證。準備、分析、Drive 上傳共用單程序工作鎖；忙碌時立即回傳可重試錯誤，不讓背景執行緒無限排隊。仍採同步處理；持久化長片佇列、Drive 配額管理、定期刪除未實作。沒有保留天數授權時不刪除任何既有影片。

Drive fallback 另需 `GOOGLE_DRIVE_FOLDER_ID`（可留白表示根目錄）及 `GOOGLE_DRIVE_REFRESH_TOKEN`、`GOOGLE_DRIVE_CLIENT_ID`、`GOOGLE_DRIVE_CLIENT_SECRET`；`GOOGLE_DRIVE_ACCESS_TOKEN` 僅適合短期測試。這些憑證只放在 MCP 主機環境，不提交到 Git；OAuth 使用者必須能讀取該資料夾，否則 ChatGPT 的 Google Drive connector 無法取回影片。Drive OAuth 僅處理儲存端授權，**不代表 MCP 公開入口已驗證呼叫者**；本版沒有更動入口驗證設定。

可靠性回歸：`python -m unittest discover -s tests -p test_mcp_reliability.py -v` 含實際 FFmpeg 影音切割／解碼、非整秒影格邊界、快取失效、Drive 中斷／重試／校驗、段數限制、容量與 SQLite 關閉檢查。Drive 部分使用模擬 API；不等於已完成線上 Drive 或 ChatGPT 新版 widget 驗收。

Drive 工具只回傳結構化檔案資訊與文字說明，不把私有 `webViewLink` 包裝成 MP4 `resource_link`。檢視頁回傳的是 HTML；必須在未啟用 developer MCP 的一般對話，由已授權的 Google Drive connector 執行 `fetch(download_raw_file=true, include_base64=false)` 取得原始影片，避免在 developer MCP 對話觸發 `FORBIDDEN`，或把檢視頁誤當影片。

## 批次建立頻道知識庫

先建立 URL 清單；每行一個影片 URL，空白行與 `#` 開頭的註解會忽略，重複 URL 會去除：

```powershell
yt-dlp --flat-playlist --print '%(webpage_url)s' --skip-download 'https://www.facebook.com/...' > urls.txt
yt-dlp --flat-playlist --print '%(webpage_url)s' --skip-download 'https://www.youtube.com/@頻道名/videos' > urls.txt
python pipeline.py --url-file .\urls.txt --slide-extractor steipete --sync-open-notebook --notebook-id 'notebook:abc'
```

批次會逐支重用同一條單片管線，輸出 `batches\<雜湊>\batch-manifest.json`；單支失敗會記錄並繼續處理其他 URL。先用 `--dry-run` 或 `--local-only` 可確認本機產物，不會下載或上傳雲端。[MiniMax 官方說明](https://platform.minimax.io/docs/token-plan/intro#after-reaching-the-usage-limit) Token Plan 超額時，若帳戶有 Credits，可能自動用 Credits 補足；本工具無法保證供應商不扣款，因此批次雲端預設停用，只有在主控台確認額度／支出控制後明確加 `--allow-batch-cloud` 才會啟用。

## RSS 自動收件匣

RSS、Atom 與 Podcast Feed 會記住 GUID、避免重複排入，支援標題關鍵字、標籤與 latest-only。Podcast 只接受影音 MIME、`medium=audio/video` 或已知影音副檔名的 enclosure；封面圖不會被當成節目。新增訂閱會立即刷新，但只建立本機佇列，不會自動消耗 Groq／MiniMax 額度：

```powershell
python pipeline.py --rss-add 'https://www.youtube.com/feeds/videos.xml?channel_id=UC...' --rss-latest-only --rss-tags '教學,AI'
python pipeline.py --rss-list
python pipeline.py --rss-refresh
```

### Facebook 作者影片追蹤

Meta Graph API 可盤點你有權管理的 Facebook Page，會同時讀取 `videos` 與 `video_reels`，用 cursor 持續分頁，沒有歷史總數硬上限。先將 Page Access Token 與目前 API 版本填入專案 `.env`，不要把 Token 貼進命令或提交到 Git：

```dotenv
FACEBOOK_PAGE_ACCESS_TOKEN=
FACEBOOK_GRAPH_API_VERSION=v26.0
```

先只盤點最近一年；這一步只呼叫 Meta metadata API，不下載影片，也不呼叫 Groq／MiniMax：

```powershell
python pipeline.py --facebook-author 'https://www.facebook.com/<PAGE>/videos' --facebook-since 365d --facebook-discover-only
```

確認結果後加入既有佇列；`--facebook-since all` 可回溯 API 實際仍可存取的全部歷史：

```powershell
python pipeline.py --facebook-author 'https://www.facebook.com/<PAGE>/videos' --facebook-since 365d --rss-all-items
python pipeline.py --rss-refresh
python pipeline.py --rss-process --rss-max-items 4 --allow-batch-cloud
```

盤點快照保存在受 ACL 保護且 Git 忽略的 `rss\facebook-<雜湊>-latest.json`。後續刷新從最新已見發佈時間繼續，穩定影片 ID 會交給既有去重、關鍵字、標籤、重試與 dead-letter 流程。Graph API 只涵蓋 Token 權限可讀的 Page 內容；已刪除、私人、過期、權限外內容及一般個人帳號不保證可取得。任意公開作者若有 RSS／Atom／RSSHub Feed，應直接使用 `--rss-add`；本工具不以脆弱的 Facebook HTML 爬蟲假裝完整。

非自管的公開 Facebook Page 可選 Apify 官方維護的 Facebook Posts Scraper；它會產生額度費用，所以除了專案 `.env` 的 Token，還必須設定單次供應商費用上限並明確加 `--allow-batch-cloud`：

```dotenv
APIFY_API_TOKEN=
APIFY_FACEBOOK_ACTOR=apify~facebook-posts-scraper
APIFY_FACEBOOK_RESULTS_LIMIT=1000000
APIFY_MAX_TOTAL_CHARGE_USD=1.00
APIFY_RUN_TIMEOUT_SECONDS=1800
```

```powershell
python pipeline.py --facebook-author 'https://www.facebook.com/<PUBLIC_PAGE>/' --facebook-provider apify --facebook-since 365d --facebook-discover-only --allow-batch-cloud
python pipeline.py --facebook-author 'https://www.facebook.com/<PUBLIC_PAGE>/' --facebook-provider apify --facebook-since 365d --rss-all-items --allow-batch-cloud
```

Apify Token 一律放在 `Authorization` header，不放 URL、輸出或 Git。Actor 只負責發現公開貼文，`captionText=false`，實際影音仍由 yt-dlp 下載並交給 Groq 轉錄、MiniMax 整理。官方目前的輸入 schema 明載此 Actor 只支援公開 Page、不支援 personal profile，因此個人帳號仍列為未保證支援；私人／登入限定內容不處理。`APIFY_FACEBOOK_RESULTS_LIMIT` 是 Actor 必填的單次結果預算，本工具取回資料集時會持續分頁、不另行截斷；Actor 的結果完整性仍受 Facebook 可見性與供應商能力限制。請參考 [Actor 輸入](https://apify.com/apify/facebook-posts-scraper/input-schema)、[API](https://apify.com/apify/facebook-posts-scraper/api) 與 [計價](https://apify.com/apify/facebook-posts-scraper/pricing)。

### Instagram 作者 Reel 追蹤

公開 Instagram 作者使用 Bright Data Instagram Scraper API 發現 Reels；API Key 只放在 Bearer header，不寫入 URL、輸出或 Git：

```dotenv
BRIGHTDATA_API_TOKEN=
BRIGHTDATA_RUN_TIMEOUT_SECONDS=1800
```

先用少量結果盤點；這只取得公開 Reel metadata，不下載影片，也不呼叫 Groq／MiniMax：

```powershell
python pipeline.py --instagram-author 'https://www.instagram.com/<USERNAME>/' --instagram-max-items 3 --instagram-discover-only --allow-batch-cloud
```

確認後加入佇列。預設不設結果筆數上限；`--instagram-since 365d` 會在本機依供應商實際回傳的發佈時間保留近一年結果：

```powershell
python pipeline.py --instagram-author '<USERNAME>' --instagram-since 365d --rss-all-items --allow-batch-cloud
python pipeline.py --rss-refresh --allow-batch-cloud
python pipeline.py --rss-process --rss-max-items 4 --allow-batch-cloud
```

盤點快照保存在 Git 忽略且受 ACL 保護的 `rss\instagram-<雜湊>-latest.json`，包含原始 Bright Data 結果與 snapshot provenance。刷新時會把已見 Post ID 傳入 `posts_to_not_include`，本機佇列仍以穩定 ID 去重。Bright Data 的 Reel discovery 目前不支援伺服器端日期篩選；若某筆沒有 `date_posted`，工具會保留並在快照及盤點訊息標示「無法驗證回溯範圍」，不會假裝近一年盤點完整。私人／登入限定內容不處理，平台可見性與供應商回傳仍可能造成缺漏。請參考 [Instagram Scraper API](https://docs.brightdata.com/datasets/scrapers/instagram/introduction)、[Reel discovery](https://docs.brightdata.com/api-reference/scrapers/social-media-apis/instagram-reels-discover-by-url) 與 [非同步 snapshot 流程](https://docs.brightdata.com/api-reference/rest-api/scraper/asynchronous-requests)。

單篇公開 Reel／影片貼文不需要先建立作者訂閱，可直接走既有入口：

```powershell
python pipeline.py --url 'https://www.instagram.com/reel/<SHORTCODE>/'
```

Bright Data 只負責作者內容發現；實際下載仍由 yt-dlp 完成，之後才是 Groq 時間戳轉錄與 MiniMax 摘要。免費額度與餘額續用行為依帳戶主控台為準，程式無法保證不扣款，因此所有 Bright Data 盤點都要求明確加上 `--allow-batch-cloud`。

### Instagram 下載與診斷

在執行 Pipeline 的虛擬環境安裝／更新：`python -m pip install -U "yt-dlp[default,curl-cffi]"`。虛擬環境內已安裝的套件優先於 PATH／VidBee 的獨立執行檔，避免更新了錯誤的版本。依 [yt-dlp 官方更新通道與 impersonation 文件](https://github.com/yt-dlp/yt-dlp#update-channels)，如需比較 nightly，請先在獨立測試環境使用 `python -m pip install -U --pre "yt-dlp[default,curl-cffi]"`，不要在每次下載時自動更新。

Instagram 先以無登入方式下載；可用時採用 `curl_cffi` 的 Chrome impersonation，忽略外部 yt-dlp 設定與外掛，避免暗中帶入 cookies。公開下載失敗後，僅在 `YTDLP_INSTAGRAM_COOKIE_FILE` 明確設定時嘗試一次 authenticated fallback。此變數須指向使用者自行提供、位於 repo 外的 Netscape cookies 絕對路徑（建議 Unix 權限 `600`）；只複製 Instagram 網域且未過期的 cookies 到私人暫存檔，結束即清理，不覆寫原檔、不讀取瀏覽器、不傳送憑證給 ChatGPT。需要時可設定 `YTDLP_INSTAGRAM_USER_AGENT`，通常留白交給 impersonation 管理。

每次實際下載在工作目錄保存 `instagram-download.json`：包含實際 yt-dlp 版本、request handlers、`curl_cffi` 版本、impersonation 能力、User-Agent 設定模式、Python／作業系統／容器標記、是否使用 cookies，以及每次嘗試的結果。失敗也會在 MCP 的錯誤文字回傳相同診斷，保留第一次失敗原因；不輸出 Cookie／Authorization 值或憑證路徑。錯誤不等於影片已刪除；cookies 也不保證能修正上游 extractor／帳戶／網路限制。Regression：`python -m unittest discover -s tests -p test_instagram_download.py -v`。

### Threads 單篇影片與 carousel

公開 Threads 貼文可直接使用同一個入口，支援 `threads.com`／`threads.net` 的 `/@user/post/<ID>` 與 `/t/<ID>` 連結：

```powershell
python pipeline.py --url 'https://www.threads.com/@zuck/post/DHV7vTivqWD'
```

Threads 不依賴 yt-dlp extractor；管線會以公開 crawler headers 取得 `data-sjs` 頁面資料，解析 `video_versions`／影片 CDN URL，再交回既有 FFmpeg、Groq 與 MiniMax 流程。影片 carousel 會下載其中的影片項目並串成一支 MP4，圖片項目會略過。頁面與媒體請求會尊重 `Retry-After`，對 HTTP 429／5xx 做有限次退避重試；私人、登入限定、已刪除或純文字／圖片貼文會明確失敗，不會要求遠端 MCP 保存 Threads 登入憑證。

沒有音訊的 Threads 影片會略過語音轉錄、保留視覺摘要；短於取樣間隔的影片會至少保留第 0 秒影格。

訂閱可修改、暫停、恢復，也可顯示逐筆狀態：

```powershell
python pipeline.py --rss-update <ID> --rss-name '新名稱' --rss-keywords 'AI,Python' --rss-tags '技術' --rss-all-items
python pipeline.py --rss-disable <ID>
python pipeline.py --rss-enable <ID>
python pipeline.py --rss-list --rss-list-items --rss-item-limit 20
```

設定修改套用到後續刷新；既有 GUID 若尚未完成，標題、媒體 URL、來源頁、發佈時間與標籤會跟著 Feed 更新。

處理佇列會直接重用既有的下載、Groq 轉錄、MiniMax 畫面核對與摘要管線；預設一次只處理一筆：

```powershell
# 只預演，不下載、不呼叫雲端 API，項目仍留在佇列
python pipeline.py --rss-process --dry-run

# 真實處理；必須明確承擔 Token Plan 超額後可能使用 Credits 的風險
python pipeline.py --rss-process --allow-batch-cloud

# 前景監看；失敗項目到冷卻時間後再重試
python pipeline.py --rss-watch 900 --rss-process --rss-retry-failed --allow-batch-cloud
```

- `--rss-keywords '關鍵字1,關鍵字2'`：標題至少命中一個才排入。
- `--rss-latest-only`：每次刷新只排入最新一個未見項目，舊項目記為 skipped。
- `--rss-max-items N`：限制本次實際處理數量；預設 1。
- `--rss-retry-failed`：只重試已到冷卻時間的失敗項目；最多三次，401／403、額度不足或三次耗盡會進 `dead-letter`。
- `--rss-requeue <ITEM_ID>`：人工確認問題已解除後，明確重排 dead-letter。
- `--rss-remove <ID>`：移除訂閱與佇列紀錄，但保留已產生的 `jobs\`。

每個 `--root` 有單一寫入鎖；項目在 API 前先持久化為 `processing`，程序中斷超過兩小時會復原成 failed。排程跨 Feed 輪流取一筆，不會讓單一大量 Feed 長期壓住其他來源；Feed 抓取最多四路並行，Groq／MiniMax 項目仍依額度限制逐筆啟動。

匯入／匯出、通知、清理與 Windows 背景排程：

```powershell
python pipeline.py --rss-import-opml .\subscriptions.opml
python pipeline.py --rss-export-opml .\subscriptions.opml
python pipeline.py --rss-cleanup-days 90

# 可選通知；JSON 只含項目狀態與已移除 query 的來源 URL
python pipeline.py --rss-process --allow-batch-cloud --rss-webhook 'https://example.com/hook'

# 預設背景排程只刷新本機佇列，不呼叫雲端
python pipeline.py --rss-task-install 15

# 必須三個旗標同時出現才會建立含雲端處理的排程
python pipeline.py --rss-task-install 15 --rss-task-process --allow-batch-cloud
python pipeline.py --rss-task-remove
```

狀態保存在 Git 忽略的 `rss\state.json`，並在 Windows 將 `rss\` ACL 限制為目前使用者、SYSTEM 與 Administrators；上一版狀態另存 `state.json.bak`。媒體 URL 的 query／userinfo 只保留在受保護狀態檔供下載，終端、manifest、通知與 yt-dlp info sidecar 只留公開 URL。Podcast 音訊模式不執行畫面分析；遇到語音提及「如圖」只會標示需核對畫面。一般說明頁或 Podlink HTML 頁面不是 Feed。

JSON 狀態仍是最短且可備份的方案；`--rss-cleanup-days` 會清掉舊的完成／過濾紀錄。只有實際超過 10,000 筆時 `--rss-list` 才提示評估 SQLite，本次沒有為空狀態引入不必要的資料庫遷移。背景排程與 Webhook 都是 opt-in；安裝專案不會自動啟用它們。

2026-09-01 真實 canary 使用公開的 1 Minute Podcast Feed，只處理 `#85 - New York Minute` 一筆：121.304 秒音訊由 Groq `whisper-large-v3` 產生 18 段時間戳，再由 MiniMax `MiniMax-M3` 產生 5 題複習問答與有效片段引用。輸出具備 `source.json`、原始／閱讀版逐字稿、SRT 與 `notes.md`；音訊 Feed 的畫面事件為 0，筆記只把畫面相關語句列為待核對。相同佇列重跑為 `selected 0`，沒有再次呼叫供應商。

## 完整流程

虛擬環境內優先使用已安裝的 yt-dlp 套件；其他情況仍可重用 PATH／VidBee 隨附的 `yt-dlp.exe`。Instagram 建議安裝：

```powershell
python -m pip install -U "yt-dlp[default,curl-cffi]"
```

複製 `.env.example` 為 `.env`，填入 Groq 金鑰、MiniMax Token Plan Subscription Key、帳戶平台 Base URL 與已確認可用的模型。`.env` 已由 Git 忽略；Windows 可用下列命令限制 ACL：

```powershell
icacls .env /inheritance:r /grant:r "$env:USERNAME:(F)" "SYSTEM:(F)" "Administrators:(F)"
```

在 VidBee 下載完成後，最短操作方式：

```powershell
python pipeline.py --video 'D:\path\to\vidbee-download.mp4' --vidbee-sidecar
```

畫面抽取器預設是零額外安裝的 `native`；要使用已驗證的場景偵測＋Tesseract OCR，指定：

```powershell
python pipeline.py --url 'https://www.facebook.com/...' --slide-extractor steipete
```

這會使用 PATH 上的 `summarize`，否則透過 `npx -y @steipete/summarize` 執行。Node 24+ 與 Tesseract 必須可用；專用 `slides` 子命令只處理畫面，不會重複呼叫 Groq 轉錄。

必要環境變數：

```text
GROQ_API_KEY=...
GROQ_ASR_MODEL=whisper-large-v3
MINIMAX_SUBSCRIPTION_KEY=...
MINIMAX_BASE_URL=https://api.minimax.io/v1  # 中國站改用 https://api.minimaxi.com/v1
MINIMAX_MODEL=帳戶實際可用模型
```

`.env` 會覆蓋繼承來的環境變數（只有非空值才覆蓋）。這點很重要：`MINIMAX_BASE_URL` 這種通用名稱常被別的工具設成不相容的值（例如指向 `/anthropic` 相容端點），若讓全域設定贏過專案設定，照著這份 README 建 `.env` 也修不好。`.env` 同時會從**專案目錄**與 `--root` 指定的輸出目錄讀取，後者優先。

逐字稿語言預設 `auto`；已知單一語言時才用 `--language zh` 等 ISO-639-1 值提升準確度。程式固定呼叫 Groq `audio/transcriptions`，不會呼叫會翻成英文的 `translations`：

```powershell
python pipeline.py --url '...' --language auto
```

### 轉錄後端

主要方案固定為 Groq；舊的 `--transcribe-backend local` 僅為相容性保留，不會預設下載本機 Whisper 模型。

```powershell
python pipeline.py --url '...' --transcribe-backend local --whisper-model small
```

模型大小用 `--whisper-model`（`tiny`／`base`／`small`／`medium`／`large-v3`）；Groq 那邊用 `--groq-model`。同一段英文音訊實測，原文是 *"here we are in front of the elephants … really long trunks"*：

| 後端／模型 | 開頭 | 關鍵字 | 耗時 |
|---|---|---|---|
| Groq `whisper-large-v3-turbo` | one of ❌ | fronts ❌ | 28.9s |
| 本機 `base` | one of ❌ | punks ❌ | — |
| 本機 `small` | one of ❌ | trunks ✅ | — |
| **Groq `whisper-large-v3`**（預設） | **in front of** ✅ | **trunks** ✅ | 59.3s |

turbo 不只聽錯關鍵字，還把 "in front of" 縮成 "one of"，整句語意都變了。慢一倍換全對，對「處理一次、往後反覆查詢」的知識庫划算，所以預設用非 turbo 版。

預設跑 CPU。`device="cuda"` 在建構時不會報錯，缺 `cublas64_12.dll` 要等真的開始轉錄才炸，那時音訊已經切完、時間也花掉了——所以要用 GPU 得明確設 `WHISPER_DEVICE=cuda`（另需 CUDA runtime 與 cuDNN）。

可選環境變數：

```text
GROQ_ASR_MODEL=whisper-large-v3-turbo  # 只有手動選快速模式時改
GROQ_LANGUAGE=auto
GROQ_MAX_UPLOAD_MB=25
```

只做下載與本地處理可用：

```powershell
python pipeline.py --url 'https://www.facebook.com/...' --local-only
```

## 產物

- `source.json`：來源、片長、供應商、模型與處理資訊。
- `manifest.json`：狀態、設定、快取與產物索引。
- `audio\audio_*.mp3`：單聲道 16 kHz、每段預設 15 分鐘。
- `frames\frame_*.jpg`：定時截圖；`frame_candidates.json` 記錄去重與 OCR 判斷。
- `transcript.raw.json`：每個 Groq 片段的完整原始 API 回應與真實來源偏移。
- `transcript.json`：合併後的結構化逐字稿。
- `transcript.txt`／`transcript.srt`：含時間戳閱讀版與字幕；`transcript.zh-TW.*` 是另存的繁體版。
- `visual.json`：MiniMax 結構化畫面觀察。
- `timeline.json`：語音與畫面事件依時間排序。
- `summary.json`：MiniMax 摘要、章節、步驟、工具名詞與待查證資訊。
- `notes.md`：繁體中文摘要、重點、五題複習問答與待核對片段。
- `knowledge.md`：相同內容加完整時間軸，也是匯入 Open Notebook 用的格式。

`--vidbee-sidecar` 另寫 `<影音檔名>.zh-TW.srt` 與 `<影音檔名>.notes.md` 到影音同目錄。字幕可供 VidBee 正式 sidecar captions 匯入並搜尋、跳轉；筆記是方便與原片一起保存的外部檔案，VidBee 不會自動將它顯示成 Prompt 結果。已有不同檔案時不覆寫。

[VidBee AI Prompts](https://vidbee.org/docs/ai-prompts/) 可在介面內用自訂 OpenAI-compatible Provider 處理已存在的逐字稿；[原生轉錄文件](https://vidbee.org/docs/transcribe/) 仍只描述本機 ASR。目前官方沒有文件化的外部 CLI／擴充介面可自動觸發 prompt run 並匯出 `notes.md`。因此本單一指令流程保留原有的 MiniMax 直接呼叫，只負責產生可回查檔案，不修改 VidBee 的內部資料庫。

MiniMax 官方目前標示 MiniMax-M3 為 1,000,000 token context、M2.x 為 204,800；本工具仍用保守的每段 120,000 字元切分並保留 `segment_id`，不會因模型更換而靜默截斷。OpenAI-compatible 呼叫使用非串流 `chat/completions` 與 `reasoning_split=true`，正式筆記只取 `message.content`，不混入 reasoning 欄位。

專案根目錄另有 `library.db`：跨影片的全文索引，所有影片共用一份。

PaddleOCR 與 PySceneDetect 暫不列為必要依賴：`native` 模式提供本地 fallback；`steipete` 模式提供場景偵測與 Tesseract OCR。兩者都只把候選畫面交給後續 MiniMax 視覺階段。

## 本地搜尋索引（本專案的資料庫）

跑過的影片會自動寫進專案根目錄的 `library.db`（SQLite FTS5，標準函式庫，無額外安裝，不需要任何服務或 port）。這一個檔就是整個知識庫，複製走即完整搬移。要重掃既有的 `jobs\`：

```powershell
python pipeline.py --index
python pipeline.py --search '時間軸合併'
python pipeline.py --search 'ffmpeg' --search-limit 50
```

搜尋涵蓋影片標題、逐字稿、畫面觀察與摘要，結果附時間戳與來源 URL。索引用 `trigram` 斷詞：`unicode61` 會把整句中文吃成單一 token，中文查詢一律落空。

`trigram` 的索引窗口是 3 字元，`AI`、`影片` 這類兩字詞 `MATCH` 必定落空，因此短於 3 字元的查詢會自動回退全表掃描（輸出會標示模式），並依「標題 → 摘要 → 逐字稿／畫面」與命中次數排序。實測 5 萬筆片段約 80 ms、線性成長，沒有效能疑慮：

```powershell
python pipeline.py --search 'AI'      # 命中 N 筆（全表掃描：查詢短於 3 字元）
python pipeline.py --search '時間軸'   # 命中 N 筆（索引）
```

`library.db` 可直接用任何 SQLite 工具開；`videos` 表一支影片一列，`events` 表是全文索引。

## 問答與總覽

兩者都需要 `MINIMAX_SUBSCRIPTION_KEY`，不需要額外服務。

```powershell
python pipeline.py --ask '這些影片有提過怎麼設定 ffmpeg 嗎？'
python pipeline.py --digest
```

`--ask` 先讓模型把問句拆成檢索詞（整句丟進 FTS5 是 phrase 比對，長問句必定 0 筆），再用檢索到的片段生成答案，並標註 `[1]`、`[2]` 對應到影片標題與時間戳。提示詞限定只能引用片段內容，資訊不足時會直說找不到，不會臆造。

`--digest` 把各支影片的摘要合成 `digest.md`：主題分群、跨影片反覆出現的術語、建議觀看順序、涵蓋落差。只吃跑過完整流程的 job，`--local-only`／`--dry-run` 的 job 沒有真摘要會被跳過。

> 呼叫 MiniMax 一律不設 `max_tokens`。MiniMax-M3 是 reasoning 模型，`<think>` 會先吃掉配額，設了上限常常在真正的答案輸出前就用完，回傳半截 JSON。

## 語意檢索

關鍵字檢索只找得到字面相同的詞。補上向量後，「厚皮動物」也能命中講大象的片段：

```powershell
python pipeline.py --embed                          # 補算向量，只算還沒有的
python pipeline.py --search '厚皮動物' --semantic
python pipeline.py --ask '他對長鼻子有什麼評論？' --semantic
```

兩種後端，用 `--embed-backend` 切換：

| 後端 | 預設模型 | 需要 | 成本 |
|---|---|---|---|
| `openrouter`（預設） | `nvidia/nemotron-3-embed-1b:free`（2048 維） | `OPENROUTER_API_KEY` | **0**（實測 `cost=0`） |
| `local` | `BAAI/bge-m3`（1024 維） | `sentence-transformers` + `torch` | **0**，且離線、內容不外傳 |

```powershell
python pipeline.py --embed                        # 免費雲端
python pipeline.py --embed --embed-backend local  # 本機
```

其他可用模型：`baai/bge-m3`、`qwen/qwen3-embedding-8b`（皆 US$0.01/M）、`nvidia/llama-nemotron-embed-vl-1b-v2:free`。

OpenRouter 的 `/embeddings` 與 `/rerank` 端點在模型清單裡查不到、官方文件也沒寫，但實測可用。要注意 chat 的 `:free` 清單與 embedding 的模型池是分開的：chat 那些 id 打 embeddings 會回 `does not exist`；而 NVIDIA 那幾顆**必須加 `:free` 後綴**才有 provider，不加會回 `No endpoints found`。

本機後端首次使用要下載約 2.2 GB 模型。CPU 實測約 94 筆/秒；裝的若是 `torch+cpu` 就吃不到 GPU，要 GPU 得另外裝 CUDA 版。

`--embed` 只補算「這個模型還沒算過」的片段：重跑不重複付費，換模型則自動重算。`vectors` 表記錄了模型名稱，查詢時只用相符的向量——混用不同模型的向量算出來的相似度沒有意義。

### 為什麼不直接嵌入截圖

`nvidia/llama-nemotron-embed-vl-1b-v2:free` 確實能讀圖（把 data URL 直接放進 `input` 即可，token 數會隨圖片變化；實測動物園實景 vs 投影片 = 0.13、投影片 vs 投影片 = 0.63，圖文對齊也正確）。

但實測**嵌 MiniMax 的文字描述比直接嵌圖更準**：

| 查詢 | 直接嵌圖 | 嵌文字描述 |
|---|---|---|
| 穿紅色外套的男生 | 0.267 | **0.331** |
| 金屬欄杆 | 0.065 | **0.192** |
| 動物在吃東西 | 0.196 | **0.300** |

原因是雙重劣勢：查詢是中文，而該模型的圖文對齊在英文遠強於中文，「圖片→中文查詢」同時跨模態又跨語言；而 MiniMax 產出的繁中描述與查詢是同模態同語言。若你的查詢主要是英文，這個結論可能反過來。

### 相似度門檻

各家模型的分數尺度差很大，實測同一組語料：

| | 正確命中 | 無關對照 |
|---|---|---|
| `baai/bge-m3` | 0.61–0.75 | **0.34** |
| `nvidia/nemotron-3-embed-1b:free` | 0.29–0.77 | **0.004** |

寫死絕對門檻必定誤殺其中一種，所以預設取「最高分的一半」再加一個很低的地板（`SIMILARITY_FLOOR`）。要固定值就用 `--min-similarity`。

輸出會標示相似度分數，低於 `0.35` 的視為不相關直接濾掉（`pipeline.py` 的 `MIN_SIMILARITY`）—— 少了下限，每次查詢都會回傳整個索引，末幾筆只是「最不像的那些」而不是命中。

`--ask --semantic` 會直接拿問句比對語意，跳過「先抽關鍵詞」那一步：少一次 API 呼叫，命中也更準。

## Open Notebook 知識庫

> **選用，預設不啟用。** 開源自架的 Open Notebook（不是 Google NotebookLM），要另外用 Docker 跑起來。本專案的資料庫是上面那個 `library.db` 單檔，查詢用 `--search` 就夠；只有想要筆記介面與 RAG 問答時才需要這一節。以下步驟經實測可用，但沒跑這套服務完全不影響管線。

部署（`docker-compose.yml` 需要 `lfnovo/open_notebook:v1-latest` 與 `surrealdb/surrealdb:v2`）：

```powershell
docker compose up -d
# API 就緒的判準是 http://127.0.0.1:5055/docs 回 200，不是容器 Running
```

SurrealDB 不必映射到 host（`open_notebook` 走 docker 內部網路 `ws://surrealdb:8000/rpc`）。少了這層映射還能避開 host 上 8000 埠的衝突。

API 路徑經實測（v1-latest）：上傳是 `/api/sources`，處理狀態輪詢是 `/api/commands/jobs/{command_id}`，且 `command_id` 必須保留 `command:` 前綴。認證用 `Authorization: Bearer <OPEN_NOTEBOOK_PASSWORD>`。



每支影片會產生一份 `knowledge.md`，包含摘要、章節、操作步驟、OCR、畫面觀察與時間軸。將多支影片指定到同一個 Notebook，即可把頻道累積成可搜尋知識庫。

先在 Open Notebook 建立 Notebook，取得 `notebook_id`，並設定本機 API 密碼：

```powershell
$env:OPEN_NOTEBOOK_PASSWORD = '只放在目前 PowerShell 工作階段'
python pipeline.py --url 'https://www.facebook.com/...' `
  --slide-extractor steipete `
  --sync-open-notebook `
  --notebook-id 'notebook:abc'
```

也可用環境變數設定：

```text
OPEN_NOTEBOOK_URL=http://127.0.0.1:5055
OPEN_NOTEBOOK_SOURCES_PATH=/sources
OPEN_NOTEBOOK_NOTEBOOK_ID=notebook:abc
OPEN_NOTEBOOK_PASSWORD=不要提交到版本控制
```

同步是明確的 `--sync-open-notebook` 選項；dry-run 與 local-only 只產生 `knowledge.md`，不會上傳。

重跑同一支影片是**替換**而非新增：`manifest.json` 的 `open_notebook.source_id` 記著上次建立的條目，下次同步會先刪掉它再上傳，`replaced` 旗標會標示有沒有真的替換掉。少了這層，每次重跑都會在 notebook 疊一份同名 source。

兩個前提：`manifest.json` 不能刪（那是唯一的對應記錄），以及若你在 Open Notebook 介面手動刪過該條目，這次同步會刪不到（404 會被忽略）而新增一份。手動刪掉的孤兒條目只能自己在介面清。
