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

Groq 轉錄與 MiniMax 畫面分析會同時執行；超長逐字稿的 MiniMax 分段摘要也會受限並行。`--workers` 預設／最多 4。不會為了速度切換到較不精確的 Whisper turbo。`--dry-run`／`--local-only` 不呼叫雲端，只在本機擷取畫面做 OCR 預覽（`--max-vision-frames N` 控制張數），這些畫面不會交給 MiniMax。MiniMax-M3 摘要使用官方 `thinking: {"type":"disabled"}` 直接回答，畫面分析仍保留 adaptive thinking。

 視覺分析只有一種方式：整支 MP4 經 MiniMax Files API 上傳後以 `video_url` 交給 MiniMax-M3 直接理解。抽幀模式與抽幀備援已移除，設定 `ANALYZE_VISUAL_MODE=frames` 會直接報錯。可調 `ANALYZE_VIDEO_FPS`（0.2–5）與 `ANALYZE_VIDEO_DETAIL`（low|default|high）。來源超過 Files API 約 512MB（或上傳因大小被拒）時，會先用 ffmpeg 壓到約 400–480MB 再重試；壓縮失敗或仍過大則該次畫面分析失敗（只保留逐字稿），不會改用抽幀；壓縮成功時以 `analysis_method=minimax_video_compressed` 標明。MiniMax 回應被截斷（`finish_reason=length`）時標示未完成、不寫入快取。`video_mp4` 的畫面時間碼一律是從 0 起算的原片秒數：提示詞要求事件覆蓋全片到片尾，解析接受純數字與 `MM:SS`／中文時間並丟棄無法解析的事件（不再靜默記為 0.0）；事件峰值不到 `min(片長-30 秒, 片長一半)` 或超出片長 30 秒以上時只重問一次（沿用已上傳的檔案 ID，短片豁免），仍對不上就以 `finish_reason=timestamp_coverage` 標示未完成、不寫入可重用快取。MCP `analysis/result.json` 只有在畫面無未完成標示、且逐字稿無剩餘斷層（`gap_check.remaining`）與補轉失敗（`backfill_errors`）時才會以 `status=complete` 寫入並被重用；否則記為未完成，重跑會再次呼叫模型。

`reasoning_split` 只改回傳格式，不能關閉推理。

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

`mcp_server.py` 是 ChatGPT Apps/MCP 入口：公開影片 URL → 既有下載器 → FFmpeg 分段 → Groq 語音辨識／MiniMax 整支影片畫面分析 → ChatGPT 根據工具證據整理時間軸。v14 widget 在準備完成後自動同時啟動伺服器分析（A）與 Drive 上傳（B，已設定時），兩者互不等待；再由「交給 ChatGPT 整理」送出整理要求；重新開啟不會自動重跑或重送。CLI 的 MiniMax 摘要流程不變；MCP 不另呼叫 MiniMax 產生摘要。影片放在 Git 忽略的 `mcp_jobs\`，工作佇列及簽章金鑰在私有 `.video-state/`。

### v14：背景工作、可核對證據與儲存管理

準備／分析／Drive 工具支援 `background=true`；可用 `get_video_job` 查進度，`cancel_video_job` 取消，`resume_video_job` 續跑，`get_prepared_video` 恢復結果及更新下載網址。新介面使用背景模式，舊客戶端的同步預設仍相容。準備完成後 widget 自動同時送出分析與 Drive 背景工作（Drive 先送；paired worker 允許同一 job 的分析與 Drive 片段上傳並行，最多兩個 worker；其他工作依佇列排程）；未設定 Drive 時只自動跑分析。兩路互不等待、失敗隔離；手動按鈕保留重試入口。交接內容只放檔案 ID／網址／大小與原片分段時間，不含任何 A 的結果，並提供「一鍵複製」與「開新一般對話」。

A 分析完成時，本機產生 `A_server_analysis.md`（逐字稿、畫面時間軸、品質與待整理摘要），widget 自動取得報告快照並提示等待 ChatGPT 整理摘要。「交給 ChatGPT 整理」要求 ChatGPT 先讀取目前證據與報告綁定，再呼叫 `finalize_analysis_report` 提交摘要及來源註記；也可在 widget 明確填寫後提交。只有 `summary_status=caller_submitted`、影片片段已在 Drive 且兩路 worker 已停止，才會排入獨立的報告上傳工作，放進同一個 Drive 資料夾。widget 追蹤 queued／running 報告；`report_ready` 且 `verified` 時顯示「A 已完成」並啟用「複製合併指令」。指令以「合併：」開頭，保留 file_id、SHA-256 與未獨立驗證聲明。

畫面由 MiniMax 讀取整支影片，舊版抽幀結果讀取時會被剔除並要求重新分析。快取比對來源 SHA-256、模型、語言、後端、整片分析設定及程式版本。介面可切換逐字稿／畫面觀察、點時間碼核對原片，並顯示證據缺漏限制。

簽名下載網址預設 24 小時有效；受管媒體預設保留七天，總量 20 GiB、單工作 4 GiB、保留至少 1 GiB 空間。清理需 `MCP_STORAGE_CLEANUP_ENABLED=1`，只處理已明確納管且到期的工作，保護執行中工作及有效分享。舊工作不會在部署時被批次納管。設定、相容性及驗證界線詳見 [操作說明](docs/video-mcp-operations.md)。

UI URI 已升為 `ui://video-timeline-pipeline/v14-fileparams-<媒體網址 hash>.html`。更新服務後需重新整理外掛動作並產生新 widget；歷史 widget 不會自動更新。以下 v8–v11 段落保留歷史修復紀錄，操作以 v14 說明為準。

v14 的可收合「實驗：存到 ChatGPT 檔案庫（#59）」只在使用者點擊分段按鈕後下載 MP4，再以宿主 `uploadFile(file, { library: true })` 上傳；一般上傳備援也必須另行點擊，不自動重送。這是選用的用戶端路徑，主流程維持既有伺服器分析。畫面顯示 fileId、大小、耗時、現算 SHA-256 與是否取得下載網址，不顯示或保存簽章網址。另按「請模型讀這個檔案」會先取得新的 `getFileDownloadUrl`，再交給 `ingest_video_segments`；確認接收數量與檔案 ID 相符後，才將 ID 加入模型可見狀態並送出追問。缺少宿主 API、下載引用無效或 ingestion 失敗時明確停止，不自動重送。成功回傳不代表檔案已出現在檔案庫或模型能讀取：大小／型別上限、真實錯誤格式及 library 不可用時的行為均未驗證，需在真實 ChatGPT 工作階段測試。

### 原生檔案交接契約（#23）

`ingest_video_segments` 重用既有接收器，以 `_meta["openai/fileParams"] = ["files"]` 接收 MP4 或代表影格的檔案引用。依 [OpenAI 官方 file inputs 契約](https://developers.openai.com/plugins/reference#define-file-inputs)，每個 file object 宣告 `file_id`、`download_url`、`mime_type`、`file_name` 四欄，前兩欄必填。只有公開媒體網址或 ResourceLink 不能通過交接。

可選的 `segment_ranges` 與 `files` 依索引一一對應，從 prepare 結果複製原片的 `start_seconds`／`end_seconds`，保留小數秒；同一片段的影格與 MP4 可以共用範圍。起訖必須是有限數值、起點不小於零、終點大於起點；長度不符或任何檔案無效時整批拒絕。舊客戶端可省略此參數，回傳時間為 `null`，不自行猜測時間。範例參數：

```json
{
  "files": [{
    "file_id": "file-example",
    "download_url": "https://files.example/segment.mp4",
    "mime_type": "video/mp4",
    "file_name": "segment_000.mp4"
  }],
  "segment_ranges": [{"start_seconds": 0.125, "end_seconds": 119.875}]
}
```

成功的 `files_received` 僅表示接收並檢查了引用格式，回傳檔案 ID、MIME、檔名與呼叫端提供的時間；不回傳或儲存暫時下載網址，也不下載內容或啟動付費分析。結果的 `limitation` 要求模型先讀取實際檔案並指出可核對的內容；宿主無法交接／讀取時必須明確停止，禁止 metadata-only 摘要。缺少 `uploadFile` 或沒有檔案 ID 時，既有 widget 實驗入口會顯示能力／上傳錯誤。此支線不改變 v14 的伺服器分析與 Drive 流程，也不宣稱引用已被模型讀取。

prepare 與 ingestion 都宣告正式 `outputSchema`；prepare schema 包含鎖忙時的 `status=busy`、`error_code=MCP_BUSY` 與重試間隔，避免客戶端把正常忙碌狀態判為 schema 錯誤。本地完整檢查使用 `python -X utf8 -B scripts/check_offline.py`。Windows Git Bash 可直接執行同一套離線回歸，避開 Python 檢查器巢狀啟動程序的 `WinError 87`：

```bash
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p 'test_*.py' -v &&
node tests/test_widget_handoff.cjs &&
node tests/test_parallel_submission.cjs &&
node tests/test_widget_analysis_report.cjs &&
node tests/test_widget_library_experiment.cjs && git diff --check
```

| Issue #23 驗收 | 本地證據與尚缺驗收 |
|---|---|
| ingestion 實際收到 ID 與下載網址 | HTTP MCP 測試確認兩欄抵達接收器；真實 ChatGPT 呼叫仍未驗證。 |
| 代表影格可被模型讀取 | 本地只驗證 JPEG 引用契約；模型指出可核對畫面的驗收未驗證。 |
| 支援 MP4 時依分段時間摘要 | 本地驗證 MP4／影格引用與小數時間對應；真實宿主解碼及內容摘要未驗證。 |
| 交接失敗不產生 metadata-only 摘要 | 缺 ID／網址、空陣列、重複 ID、非 HTTPS 與錯誤範圍皆拒絕；widget 等待相符的 ingestion 回執後才公開模型 ID／追問，失敗及過期工作不會送出。 |
| 媒體端點、無上限分段與重啟不回退 | 沿用完整離線回歸，相關實作未變更；本地測試不代表公開端點或部署重啟驗收。 |

### MCP 影音暫存保留 7 天（有 Drive 完整備份時改為分析後清理）

有設定 Google Drive 時，Drive 是影片的長期保存處：Drive 工作會連同整支原片一起上傳並以大小與 SHA-256 驗證，送出背景分析時也會自動排入 Drive 工作。備份完整的工作不必等 7 天：分析已有結果、播放連結都已過期、最後一次準備／分析超過 `MCP_DRIVE_BACKED_GRACE_SECONDS`（預設 1 小時），且 Drive 端逐檔確認未刪除、位元組相同時，就刪除本機媒體。之後重新準備、分析或上傳報告時會自動從 Drive 取回並驗證，原有分析快取仍可沿用。建議排程每小時執行一次。

`media_retention.py` 預設只預覽；加 `--apply` 才刪除。只處理準備及分析皆完成、來源指紋一致、品質為 `evidence_available`，且工作內最後檔案寫入已**超過 7 天**的工作。這是保守的完成時間判定，後續寫入會重新延長保留期。失敗、未完成、需要覆核、有 Drive 交接紀錄、`.keep-media`、下載暫存或連結的工作全部保留。

```bash
cd /home/box/video-timeline-pipeline
.venv/bin/python media_retention.py --jobs-root /home/box/video-timeline-pipeline/mcp_jobs
.venv/bin/python media_retention.py --jobs-root /home/box/video-timeline-pipeline/mcp_jobs --apply
```

只刪除明確命名的來源影片、MP4 分段、音訊與影格檔案；不刪工作目錄、摘要、逐字稿、JSON 紀錄、付費回執或 Google Drive 檔案。刪除紀錄留在各工作的 `media-retention.json`；刪除無法復原，原有媒體網址會失效，可重新下載來源影片。清理與 MCP 共用跨程序工作鎖，忙碌時跳過、不排隊。**啟用排程前必須先部署並重啟新版 MCP**，不可讓不認識共享鎖的舊程序與清理同時執行。可排每日台灣時間 03:00 先預覽再執行；程式本身不會安裝排程。安全回歸：`python -m unittest discover -s tests -p test_media_retention.py -v`。

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

本機檢查：`Invoke-RestMethod http://127.0.0.1:8787/health`；ChatGPT Developer Mode 則新增 MCP app，填入 MCP tunnel 網址加 `/mcp`。需在 MCP 主機設定既有的 `GROQ_API_KEY` 與 `MINIMAX_SUBSCRIPTION_KEY`。`MCP_MEDIA_BASE_URL` 僅供預覽／分段連結使用；主流程不透過瀏覽器抓取 MP4。分析工具回傳來源大小／SHA-256、逐字稿與整支影片畫面時間軸，再由 widget 送出 ChatGPT 整理要求；這不是 ChatGPT 親自讀取 MP4。`ingest_video_segments` 保留舊 API 相容，widget 僅在使用者明確點擊原生上傳實驗的模型讀檔按鈕時呼叫它。

v11 的 `quality` 是保守證據檢查，不是準確率評分：分析階段失敗、有音軌卻無可用逐字稿、缺畫面描述、未完成回應，或畫面自報信心缺漏／不高於 0.5，會回傳 `needs_review`。沒有音軌不是 ASR 失敗。模型自報信心未經校準，即使 `evidence_available` 也不保證內容正確；畫面是抽樣，非逐幀檢視。品質不足時不送出一般摘要要求，改走自動上傳的 Drive 原片覆核交接內容（B 路線：一般對話用官方 connector 讀原片，保持獨立）；「開始分析」「上傳到我的 Drive」按鈕保留手動重試入口。若 ChatGPT 整理時發現矛盾或使用者認為品質差，也走此入口。工具例外或 Drive 失敗時明確停止，保留重試入口，不把 metadata 當成讀取成功。

v9 修正 developer MCP 與官方 Drive 的交接邊界：不在原對話送出 Drive 讀取／摘要 follow-up；先開啟未啟用 developer MCP 的一般 ChatGPT 對話，選擇官方 Google Drive，再貼上 widget 的交接內容（job ID、全部檔案 ID／網址／大小與原片分段時間）。新對話須實際取得 bytes、記錄大小／SHA-256 並解碼，不能把 `drive_ready` 當成讀取成功。一般對話連結不會自動改變帳號的 developer mode 或連線設定，使用者需確認對話類型。登入、OAuth、Drive 檔案權限均未變更。UI URI 含媒體網址 SHA-256 前 12 碼，避免換網址後仍使用舊 CSP 快取。

分析與 Drive 未取得單程序工作鎖時回傳 `status=busy`、`error_code=MCP_BUSY`、`retry_after_seconds=5`，不是成功，也不加入背景佇列。widget 僅對此忙碌狀態（或舊 host 包裝後的同一忙碌訊息）等待 5、10、20 秒，各重試一次；最多四次呼叫。仍忙碌就停止並保留手動重試入口。Drive 或 ChatGPT follow-up 失敗時沿用同一 widget 的分析結果，不重跑付費分析。其他錯誤不在 widget 自動重試；Drive 儲存層原有的續傳重試仍保留。切換 job 後不再重試舊工作，也不讓資料型通知覆寫目前分段。這不是持久化佇列，也沒有解除 ChatGPT 原生 `Unsupported file type` 限制。依 [官方 UI 快取規則](https://developers.openai.com/plugins/build/chatgpt-ui#embed-the-component-in-the-server-response)，UI URI 升為 `ui://video-timeline-pipeline/v11-<媒體網址 SHA-256 前 12 碼>.html`；部署後必須重新整理外掛動作並產生新 widget，歷史 widget 不會自動更新。`node tests/test_widget_handoff.cjs` 驗證流程與競態；不代表真實模型品質或網頁端驗收通過。

監督啟動器預設讓媒體網址沿用固定的 `MCP_PUBLIC_BASE_URL`，避免 Cloudflare Quick Tunnel 換名後，長時間運作的 MCP 仍回傳已失效的影片網址。既有 `MCP_MEDIA_BASE_URL` 明確設定仍優先；需要 Quick Tunnel 時可明確設 `MCP_USE_QUICK_TUNNEL=1`，此模式仍需要在網址變更後受控重載 MCP 並建立新 widget。歷史 widget 的舊網址不會自動更新。

新 tunnel 啟動會給遠端 DNS 負快取傳播時間：`MCP_MEDIA_STARTUP_ATTEMPTS` 預設 300 次（允許 1–900），每次失敗間隔 1 秒、單次連線最多 10 秒。不再僅因啟動頭一分鐘尚未解析就換掉已註冊的 tunnel。這是啟動等待，不會放寬 TLS 驗證，也不會改寫系統 DNS。

媒體 tunnel worker 現在會驗證 DNS／TLS 後才發佈網址，每 30 秒檢查公開 `/health`；連續三次失敗才重建自己的 cloudflared 子行程。來源服務尚未啟動／故障或公開端 HTTP 429 不會觸發無效的 tunnel 重建。tunnel 不可用時，啟動器忽略舊網址並退回 MCP 原有公開 origin，保留 MCP／Drive 功能，不宣稱原生附件已恢復；現有 MCP 若仍持有不同的媒體網址，啟動器回傳非零，不強制中斷正在處理的影片。網址更換後，仍須受控重載 MCP、重新整理 ChatGPT 動作並 prepare 新 widget，才能更新確切的 CSP 網域；舊 widget 須使用 Drive 交接。回歸檢查（Linux／WSL）：`python3 -m unittest discover -s tests -p test_media_tunnel.py -v`。這不等於外部 ChatGPT 能下載 MP4 的驗收；[Quick Tunnels 官方不提供 SLA，僅供開發測試](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)，固定網域部署需另行設定 named tunnel。本次不新增網域或更動公開入口。

Funnel 主機名變更後，ChatGPT 外掛清單可能殘留「Video Timeline Pipeline - Cursor Fixed」（Workspace upload，包著已刪除的 grokbot-2 App）：其選單只有編輯／下載 ZIP／上傳新版本，沒有刪除入口（#60）。該項目未安裝、不會被呼叫（2026-10-08 觀察），只是留在清單上，不影響新連線；保持未安裝即可。請改用新建立的連線，並在重新整理外掛動作後 prepare 新 widget。

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

確認後加入佇列。`--instagram-max-items` 是必填的供應商結果預算（`num_of_posts`，正整數），沒有它即使加了 `--allow-batch-cloud` 也會在送出前拒絕；`--instagram-since 365d` 只會在本機依供應商實際回傳的發佈時間保留近一年結果，不會限制供應商計費範圍：

```powershell
python pipeline.py --instagram-author '<USERNAME>' --instagram-max-items 20 --instagram-since 365d --rss-all-items --allow-batch-cloud
python pipeline.py --rss-refresh --allow-batch-cloud
python pipeline.py --rss-process --rss-max-items 4 --allow-batch-cloud
```

舊訂閱若沒有結果預算（`instagram_max_items` 為空），刷新會直接失敗並保留原訂閱，不會送出計費請求；用以下命令補上有限預算即可繼續，不需刪除重建：

```powershell
python pipeline.py --rss-update '<SUBSCRIPTION_ID>' --instagram-max-items 20
```

盤點快照保存在 Git 忽略且受 ACL 保護的 `rss\instagram-<雜湊>-latest.json`，包含原始 Bright Data 結果與 snapshot provenance。刷新時會把已見 Post ID 傳入 `posts_to_not_include`，本機佇列仍以穩定 ID 去重。Bright Data 的 Reel discovery 目前不支援伺服器端日期篩選；若某筆沒有 `date_posted`，工具會保留並在快照及盤點訊息標示「無法驗證回溯範圍」，不會假裝近一年盤點完整。私人／登入限定內容不處理，平台可見性與供應商回傳仍可能造成缺漏。請參考 [Instagram Scraper API](https://docs.brightdata.com/datasets/scrapers/instagram/introduction)、[Reel discovery](https://docs.brightdata.com/api-reference/scrapers/social-media-apis/instagram-reels-discover-by-url) 與 [非同步 snapshot 流程](https://docs.brightdata.com/api-reference/rest-api/scraper/asynchronous-requests)。

每輪作者採集的 POST 僅送一次，回執保存在所選 `--root` 的 `rss/brightdata/<雜湊>/`。相同輸入在輪詢、下載或本機儲存失敗後重試，沿用原 snapshot；只有訂閱狀態（或 `--instagram-discover-only` 的快照）成功儲存，後續明確允許的刷新才會開始新一輪採集，空結果亦同。回應不明時停止，不以重送嘗試恢復；改輸入／root 屬於另一個請求，不應用來繞過此保護。

單篇公開 Reel／影片貼文不需要先建立作者訂閱，可直接走既有入口：

```powershell
python pipeline.py --url 'https://www.instagram.com/reel/<SHORTCODE>/'
```

此 CLI 作者盤點流程的 Bright Data 只負責內容發現；實際下載仍由 yt-dlp 完成，之後才是 Groq 時間戳轉錄與 MiniMax 摘要。免費額度與餘額續用行為依帳戶主控台為準，程式無法保證不扣款，因此所有 Bright Data 盤點都要求明確加上 `--allow-batch-cloud`，且作者盤點另須以 `--instagram-max-items` 給出有限的供應商結果預算。

### MCP 公開 IG／Threads／Facebook 單篇貼文

`collect_social_post(url, allow_paid=false)` 使用 Bright Data 官方的 [Instagram Posts API](https://docs.brightdata.com/api-reference/scrapers/social-media-apis/instagram-posts-collect-by-url)、[Reels API](https://docs.brightdata.com/api-reference/scrapers/social-media-apis/instagram-reels-collect-by-url)、[Threads Posts API](https://brightdata.com/products/web-scraper/threads) 或 [Facebook Posts API](https://docs.brightdata.com/api-reference/scrapers/social-media-apis/facebook-posts-collect-by-url)，取得公開單篇文字、作者、發佈時間、可用互動數，以及供應商實際回傳的圖片／影片 CDN 網址。支援 IG `/p/`、`/reel/` 和 Threads `.com`／`.net` 單篇網址，不是任意社群通用閱讀器；不保證私人、已刪除內容、完整留言或所有 carousel 媒體。

Facebook 支援 `/<作者>/posts/<數字 ID 或 pfbid>`、`story.php`／`permalink.php`（必須同時有 `story_fbid` 與 `id`）及 `/share/p/`、`/share/v/`。`www`／`m`／`web` 網域及同一明確貼文 ID 共用快取；不解析任意重新導向或接受首頁、社團、留言網址。FB `/reel/`、`/watch/`、`/share/r/` 仍走既有影片準備流程，不送到貼文 dataset。分享 token 無法離線證明等同某個 post ID，因此分享與原始貼文網址不合併；請固定使用同一網址，避免另建採集。若供應商把分享網址換成無法核對的另一種網址，會停止交接，不猜測它是同一篇。

FB 回應的 `content` 是貼文文字；`attachments` 的照片與影片分開回傳，影片縮圖不當成 MP4。只有通過 HTTPS／Meta CDN 網域檢查的媒體網址會保留。這些網址仍未被下載、辨識或轉錄；互動數為供應商回傳的快照，不代表即時或完整留言。

此工具預設拒絕計費採集。管理員須在主機安全設定 `BRIGHTDATA_API_TOKEN`，並將人工核准的單篇網址放入 `BRIGHTDATA_ALLOWED_POST_URLS`（逗號分隔）；呼叫者另須取得使用者明確同意才設定 `allow_paid=true`。網址核准清單不等於呼叫者身分驗證，也不是金額上限，仍須在供應商設定支出控制。本版不改 MCP 登入設定；憑證不要貼到對話或提交 Git。

回傳 `pending` 時依 `retry_after_seconds` 等待，再以相同網址呼叫，會沿用同一 snapshot；`ready` 後重用本機快取。首次送出僅嘗試一次，回應遺失或供應商失敗時不自動重建計費工作，須人工到 Bright Data 核對。狀態保存在 Git 忽略的 `mcp_jobs/social-posts/`，不要為了重試直接刪除；CDN 網址可能過期，工具不自動付費刷新。

兩條 Bright Data 路徑共用提交保護：送出前在 `.submitted` 寫入時間、請求 URL／輸入與 SHA-256，送出後先存 `submission.json` 的回應或錯誤診斷，再解析 snapshot。回執不保存請求 headers，並遮蔽 API token；有效 snapshot 可在 checkpoint 寫入失敗後恢復。未知格式、損壞回執、舊版空白 `.submitted` 或回執未寫成時一律禁止重送，請依該目錄資料與供應商紀錄人工核對，不要刪除標記。

貼文文字可交給 ChatGPT 整理；`image_urls`／`video_urls` 只是媒體參照，`media_analyzed=false`，不能宣稱已看過畫面。ChatGPT 可再將 `video_urls` 逐支交給既有 `prepare_video_for_chatgpt` → Groq／MiniMax 分析 → ChatGPT 整理，並保留原始貼文網址作為來源；這不是 `download_video` 裡的隱式付費 fallback。圖片尚未自動送入視覺模型，Drive 仍為選用儲存。

不計費回歸：`python -m unittest discover -s tests -p test_social_post.py -v`。測試使用模擬供應商回應，不代表真實 Bright Data 採集、媒體下載或 ChatGPT 網頁端驗收已通過；部署新增工具後須重新整理 ChatGPT 外掛動作。

### Instagram 下載與診斷

在執行 Pipeline 的虛擬環境安裝／更新：`python -m pip install -U "yt-dlp[default,curl-cffi]"`。虛擬環境內已安裝的套件優先於 PATH／VidBee 的獨立執行檔，避免更新了錯誤的版本。依 [yt-dlp 官方更新通道與 impersonation 文件](https://github.com/yt-dlp/yt-dlp#update-channels)，如需比較 nightly，請先在獨立測試環境使用 `python -m pip install -U --pre "yt-dlp[default,curl-cffi]"`，不要在每次下載時自動更新。

Instagram 先以無登入方式下載；可用時採用 `curl_cffi` 的 Chrome impersonation，忽略外部 yt-dlp 設定與外掛，避免暗中帶入 cookies。公開下載失敗後，僅在 `YTDLP_INSTAGRAM_COOKIE_FILE` 明確設定時嘗試一次 authenticated fallback。此變數須指向使用者自行提供、位於 repo 外的 Netscape cookies 絕對路徑（建議 Unix 權限 `600`）；只複製 Instagram 網域且未過期的 cookies 到私人暫存檔，結束即清理，不覆寫原檔、不讀取瀏覽器、不傳送憑證給 ChatGPT。需要時可設定 `YTDLP_INSTAGRAM_USER_AGENT`，通常留白交給 impersonation 管理。

每次實際下載在工作目錄保存 `instagram-download.json`：包含實際 yt-dlp 版本、request handlers、`curl_cffi` 版本、impersonation 能力、User-Agent 設定模式、Python／作業系統／容器標記、是否使用 cookies，以及每次嘗試的結果。失敗也會在 MCP 的錯誤文字回傳相同診斷，保留第一次失敗原因；不輸出 Cookie／Authorization 值或憑證路徑。錯誤不等於影片已刪除；cookies 也不保證能修正上游 extractor／帳戶／網路限制。Regression：`python -m unittest discover -s tests -p test_instagram_download.py -v`。

### Facebook 下載與 cookies

Facebook 單支影片／Reels 同樣走 yt-dlp；可用時採用 `curl_cffi` Chrome impersonation。公開下載失敗（例如部分 Reel 出現 `Cannot parse data`）後，僅在 `YTDLP_FACEBOOK_COOKIE_FILE` 明確設定時嘗試一次 authenticated fallback。變數須指向使用者自行提供的 Netscape cookies 絕對路徑（建議 `/home/box/video-timeline-pipeline/secrets/facebook_cookies.txt`，Unix 權限 `600`）；只複製 Facebook／fb.com 網域 cookies 到私人暫存檔，結束即清理，不覆寫原檔、不讀取瀏覽器。工作目錄會寫入 `facebook-download.json` 診斷（不含 cookie 值）。Regression：`python -m unittest discover -s tests -p test_facebook_download.py -v`。

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

切段邊界漏轉會自動補轉（issue #55）：Groq 轉錄合併後若出現超過 5 秒的內部斷層（片頭片尾除外），會對斷層前後各取 15 秒上下文補轉一次再合併；補轉結果同樣走快取，重跑不再付費。逐字稿內的 `gap_check.remaining` 即為驗收狀態（空陣列＝無斷層）。單次最多補 8 個斷層（先補最大的）。單一斷層補轉失敗不丟整份逐字稿，只記在 `backfill_errors`；預算／取消／超時照常中斷。

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

### CutSpec / NLE handoff（research spike，issue #11）

`cutspec.py` 把有證據的片段變成可交給 Premiere/Resolve 等 NLE 的 rough-cut
規格：版本化 `CutSpec`（deterministic `specHash`）、唯讀 preview、
CSV + OTIO 匯出、`verify` 產生 `CutReceipt`（OTIO parser round-trip +
ffprobe source-range coverage）。全程 0 外部 API call、不修改來源影音、
source revision 漂移即 `STALE_SOURCE` 不自動重綁；匯出預設只留
basename（`--full-paths` 才保留完整路徑）。設計與實測 receipt 見
`docs/research/issue-11-paper-cut.md` 與 `docs/research/issue-11-receipts/`。

```powershell
python cutspec.py build --video source.mp4 --segments segs.json --out spec.json
python cutspec.py preview --spec spec.json --video source.mp4
python cutspec.py export --spec spec.json --format otio --out cut.otio
python cutspec.py verify --spec spec.json --video source.mp4 --export-file cut.otio --format otio --receipt receipt.json
```

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

不可靠的畫面時間碼（含未驗證的舊 MP4 觀察）仍可搜尋，但 `--search`／`--ask` 引用顯示「時間碼不可靠」且不提供跳轉；`--show` 與重新產生的知識檔章節／待查證資訊也會抑制落在不可靠整數秒的連結，同秒有可靠事件時保留連結。更新後請先執行 `--index` 重建本機索引。

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

## 成本守衛（#4）

下列已啟用的傳輸入口經過同一道守衛：送出前先在 `operations.db` 原子預留金額，送出後入帳；結果不明時保留預留。CLI、背景排程與 MCP 綁定同一個成本帳本。部分 HTTP helper 的內建重試目前仍合併記在一次邏輯呼叫內，尚未逐次入帳；這些缺乏可驗證費用上限的操作在 strict 模式會拒絕送出。

已接入的傳輸：

| 供應商 | 操作 | 預留時機 |
|---|---|---|
| Groq | `audio.transcriptions` | 每個音訊片段送出前（命中本機快取時不預留、不產生新費用） |
| MiniMax | `chat.completions`（整支影片畫面分析）、`summarize.*`、`query.keywords`、`query.answer`、`digest.report`、`library.classify` | 每次送出前，同一則 `finish_reason` 重試會各自入帳 |
| MiniMax | `files.upload` | 每次 upload attempt 送出前；bytes 是請求單位，未知價格記為 unknown，strict 模式拒絕送出 |
| OpenRouter | `embeddings` | 每個 batch 送出前（向量已存在時不送出、不預留） |
| Meta Graph | `graph.query` | 每次分頁請求 |
| Apify | `actor.run.start` | 只送出一次啟動 POST，不自動重試；金額以供應商端 `APIFY_MAX_TOTAL_CHARGE_USD` 封頂。timeout／5xx 保留未確認預留；後續 poll／dataset 讀取屬同一筆遠端工作，不重複計完整 Actor 費用 |
| Bright Data | `datasets.trigger` | 寫入 `.submitted` 回執前；被拒絕時不會留下「送出狀態不明」的標記 |
| Open Notebook | `sources.sync` | 建立 source 前 |
| RSS 通知 Webhook | `notify` | 每次通知前 |
| Email（Resend／SMTP／Webhook） | `email.send`、`webhook.post` | 每次寄送前，被拒絕時回 `permanent_failure`（不重試、不遞迴告警） |

**嚴格模式只放行帶有供應商端可執行上限的操作。** 目前只有 Apify Actor 啟動傳入 `maxTotalChargeUsd`（[Apify Run Actor API](https://docs.apify.com/api/v2/actors-runs-post) 說明該值限制單次 run 的總費用）。Groq、MiniMax、OpenRouter 等路徑目前只有估算，沒有可驗證的供應商端費用上限，因此即使估算值為正，hard-stop 開啟時仍會拒絕；soft mode 的估算只供記錄，不保證實際費用不超出本地預算。

```text
DAILY_BUDGET_USD=5.00
BUDGET_HARD_STOP=true
COST_TIMEZONE=Asia/Taipei
```

* `DAILY_BUDGET_USD` 留空或 `0`：不設上限，守衛只記帳。
* `BUDGET_HARD_STOP=true` **且** `DAILY_BUDGET_USD>0`：嚴格模式。`已花費 + 未結清預留 + 本次上限` 超過每日上限時，傳輸在開連線前就被拒絕（`Budget blocked:`），並記一筆 `blocked_budget`。
* 嚴格模式下成本帳本不可寫（唯讀、損壞、磁碟滿）一律 fail-closed，不會因為記帳失敗就放行付費請求。
* 超時、連線中斷或結果不明的 attempt 記為 `unknown` 且**不結清**預留，避免遠端仍在跑的工作看起來免費；之後要用 `provider_operation_id`（Apify run、Bright Data snapshot）對帳。
* 預算檢查在 SQLite writer transaction 內讀取已花費與未結清預留，commit 後才送網路；併發 worker 不會使用同一份舊餘額重複批准。
* 受守衛控制的 `request_json` 每次實際 HTTP attempt（包括重試）都有獨立預留與 usage row，以 `logical_operation_id`／`attempt_no` 連結；先前 timeout 的風險不會被後來成功的重試消除。同一筆 unknown usage 與其未結清預留只計一次風險。
* 本機快取重用記為 `cache_hit`／`zero_local`，不建立新的預留、不送出供應商請求，也不抵扣先前成本。舊帳本只增加新欄位，不重估或改寫歷史金額；缺少舊 reservation link 的紀錄保留原有保守計算與人工對帳邊界。
* `--budget-status` 顯示已結算、未結清預留與 projected 風險；`remaining_usd` 是扣除全部風險後可用的預算。整數微美元欄位保留精確值；正數但不足一微美元的上限不會變成無上限。quota／permission 業務碼在回應遮罩後分類，不當成暫時限流重試。
* 沒有開嚴格模式時，守衛是諮詢性的：帳本寫不進去（唯讀、磁碟滿、被鎖住）只會印一行 `!` 警告並照常送出，但該 attempt 的預留會留在 `reserved`，不會因為沒記到帳就當成沒花錢。要「記不到就不准花」，請開 `BUDGET_HARD_STOP=true` 並設定 `DAILY_BUDGET_USD`。
* 目前沒有自動釋出／對帳 `reserved` 預留的工具；unknown attempt 的遠端工作要人工用 `provider_operation_id` 對帳（規劃中的統一對帳屬 #7 範圍）。

```powershell
python pipeline.py --cost-report daily
python pipeline.py --budget-status
python pipeline.py --provider-health
```

報表只讀本機 `operations.db`，不會為了查成本再發任何付費請求；「本程式、指定帳戶範圍、截至 as_of」不等於供應商帳單。`unknown` 不會被當成 0。

**邊界**：本地守衛只能攔截「這個程式送出去的新請求」。供應商晚計費、同一帳戶被其他工具共用、無法取消的遠端工作，與沒有可驗證價格的操作，都不在這個保證裡；要真正的零超支，仍需要供應商端的額度控制。
