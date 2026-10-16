# 本機 Dashboard 與驗收（#6）

Dashboard 是既有 pipeline／RSS／評分／成本／Digest service 的選用介面。資料仍由本機 `library.db`、`operations.db` 與原 `rss/state.json` 保存；頁面不擁有第二套 RSS 佇列。一般 CLI 不依賴 FastAPI、Jinja2 或 Uvicorn。

## 啟動與授權

安裝 `requirements.txt`；需要介面時另裝 `requirements-web.txt`。明確執行 `python pipeline.py --root <資料目錄> --web-init` 才建立／更新索引及 Dashboard schema。這是維護操作，先停止同資料目錄的寫入程序並備份資料庫；`--serve` 不隱含初始化。

`python pipeline.py --root <資料目錄> --serve` 預設綁 `127.0.0.1:8080`、唯讀。讀取使用 SQLite `mode=ro`／`query_only`；缺少相容 catalog 時回 `migration_required`，不偷偷遷移。`first_completed_at` 保存實際首次完成／既有 `eligible_at`，舊資料沒有可信時間時保留未知，不能用重新索引時間補值。

`--serve --web-writes` 需要至少 32 字元的私人 `WEB_TOKEN`。在「操作授權」輸入該權杖後才可送出副作用 POST；權杖僅存在目前頁面記憶體。讀取會話 cookie 有一小時期限、HttpOnly／SameSite=Strict，遠端另設 Secure；寫入仍需 Bearer，不能只憑 cookie。Web CLI 不自動載入 `.env`；由呼叫程序提供 provider、預算與 `EMAIL_TO` 等環境變數。

遠端模式需全部具備：明確 `--allow-remote`、有效權杖、單一 HTTPS `--web-origin`、確切 `--web-trusted-proxy` IP。部署者須讓可信 TLS proxy 成為唯一入口並隔離後端埠；應用程式拒絕其他來源 peer，且不採信轉送 header。這些參數不會替管理者建立 TLS／防火牆；此次沒有部署遠端服務。

## 資料與請求界線

- GET 搜尋只用本機 FTS，沒有 query embedding、LLM、rerank 或模型下載。語意／混合搜尋與 Ask 只在明確 POST 後排程；Web 語意使用既有 OpenRouter embedding，Ask 使用既有 MiniMax 與成本 guard。無本機證據的 FTS Ask 直接回 `insufficient_evidence`。
- 影片預設每頁 50、最多 100；證據與歷史也有界分頁。查詢最多 500 字、Top-K 1–100、日期欄位與排序有 allowlist；類別採精確 JSON 成員比對，CLI 搜尋共用同一條件。向量搜尋沿用既有排名／cutoff，以串流 Top-K 保持 O(K) RAM，不在啟動時載入模型。
- 預設統計時區 Asia/Taipei；今日／本週 KPI 附 window、timezone、as_of、scope，連結沿用相同日期條件。影片詳情只讀 DB 已知 ID 與允許產物，摘要最大 2 MiB，原文從索引分頁；摘要／標題沒有精確時間就標「影片層級」。
- 僅掛載專用 CSS／JS 目錄；不公開資料目錄、資料庫或 `.env`。產物 resolve 後檢查 root／symlink，來源網址只接受公開平台的 http(s) metadata，移除秘密 query。模板 autoescape，模型文字以 textContent 顯示。
- Host／Origin／Sec-Fetch-Site 驗證、self-only CSP、無跨域放行；JSON body 16 KiB、讀 body 5 秒、request 10 秒、HTTP 同時最多 8、每分鐘副作用 POST 最多 30、待領取工作最多 64。錯誤附 schema_version、code、safe_message、retryable、request_id；不回傳原始例外或憑證。

## 持久化工作與恢復

`POST /api/search`、`/api/ask`、`/api/jobs/{job}/retry`、`/api/digest/send` 都需 Bearer 及 `Idempotency-Key`，先回 202／task ID。`GET /api/tasks/{id}` 只讀進度。`POST /api/tasks/{id}/cancel` 可重複呼叫；queued 轉 cancelled，running 轉 cancel_requested，禁止後續傳輸，不宣稱遠端已取消或退款。

`python pipeline.py --root <資料目錄> --web-worker` 每次只處理一筆，沒有隱藏常駐排程。`operation_tasks` 保存 library／job／revision、safe_payload、去重鍵、狀態、lease、heartbeat、checkpoint、遠端 ID 與結果；`operation_requests` 保留每個請求鍵的原始收據。同一資料目錄僅領取一筆工作，網路在短交易外執行。

lease 60 秒、每 10 秒 heartbeat。領取時用 `BEGIN IMMEDIATE`，結果寫回核對 token 與期限。重啟後只重排尚未提交的本機步驟；已 checkpoint=submitted 且失聯者轉 unknown，不自動重播。每個付費 POST 前重新核對取消與 lease，逾時／408／5xx／無法解碼回應保守保留 unknown；只有明確未接受且可重試的回應才沿用原 provider retry 規則。

unknown 必須先由操作人員核對供應商紀錄與費用；本版沒有自動遠端 reconciliation。重複鍵永遠指向原收據；新鍵可重試已確定 blocked／failed 的查詢，但 active／unknown 同內容不另建工作。明確 RSS 重排在原 single-writer lock 內再次檢查 status、cooldown 與三次嘗試上限，不歸零 attempts，不重建 unknown／dead-letter。後續分析仍由原 RSS worker 與原 cloud opt-in／cost guard 控制。

## 成本、摘要與通知

成本僅讀本程式帳本，分 actual、estimated subtotal、confirmed_included、unknown、outstanding reservations；未知不是零元，也不把小計當完整供應商帳單。日／月成本 period 可由 `/api/costs?period=daily|monthly` 查詢；預算沿用現有每日 guard，不捏造月上限、供應商配額或 reset 倒數。供應商健康顯示觀測窗口、樣本、成功／失敗、最近成功、延遲與快取率，不能推論 SLA 或美元節省。

`POST /api/digest/preview` 只讀既有摘要並組裝本機 draft，零遠端呼叫；唯讀模式不落盤，寫入模式保存 immutable draft。預覽顯示設定的 `EMAIL_TO`、provider、日／週確切窗口與候選／選入／待補數；既有即時通知覆蓋無法可靠判定時標 unknown。Web 只允許設定的單一 email；不接收字幕／LLM 提供的收件者或任意 webhook URL。

送出必須引用 `digest_id + revision + payload_hash`；內容、來源摘要、設定的 provider／收件者或儲存 payload 改變會拒絕舊確認。worker 透過既有 outbox 送出凍結的 subject／text／HTML，重試沿用相同內容與 idempotency key。只有 `retryable_not_accepted` 可由既有明確 `--notification-retry` 重試；unknown 不排自動重送。accepted 顯示「供應商已接受」，不宣稱送達；通知頁保留實際狀態。沒有可靠 bounce／delivery callback 時不自行猜測。

需補寄／重寄時勾選「明確建立新版」，重新預覽並確認新版；一般重新整理與重複確認只取得原有收據。這仍是使用者明確送出，不能用作 unknown 的自動恢復。

## 驗收對照

| 範圍 | 實作與證據 |
|---|---|
| W0／W8 | 選用 factory、唯讀啟動、Host／Origin／授權／大小限制、錯誤遮罩；HTTP tests 與缺 Web 套件 CLI 子程序驗證 |
| W1 | 穩定 catalog、時區日期與共用 filter；重建索引不改首次完成、缺分數／成本不補零 |
| W2 | Library／證據／歷史有界分頁、已知 ID 與安全產物、評分解釋、相關影片、每片工作與用量；瀏覽器分頁與手機畫面驗證 |
| W3 | GET 零 provider；明確 POST／成本 guard／202／local-first Ask；無證據、blocked、unknown／取消及未知傳輸回歸 |
| W4 | 直接讀原 RSS 狀態與時間；原 writer lock、cooldown／dead-letter 上限，排隊後狀態變更再次拒絕 |
| W5 | 共用既有 cost／budget／provider reports 的唯讀路徑，分開實際／估計／未知與觀測樣本 |
| W6 | preview 零寄送／LLM、凍結確認、設定與內容變更拒絕、重複送出一次、retry wire 內容不變、新 revision UI |
| W7 | SQLite durable claims／lease／heartbeat／fencing／checkpoint／dedup，模擬 crash、失效 lease、unknown 不重播與取消後不送下一步 |
| W9 | schema_version=1、安全錯誤 DTO、驗證輸入、穩定 idempotency receipt；API integration |
| W10 | 狀態顯示、模型 lazy load、有界原文與串流向量；1k／10k 效能基準與重跑紀錄 |
| W11 | 全部離線回歸、HTTP／fake provider／Node UI 檢查、真實 Chromium 操作與窄畫面 |

完整本機回歸（Git Bash 相容）：

```bash
python -X utf8 -B scripts/check_offline.py
python -X utf8 -B scripts/check_dashboard_benchmark.py --check
```

本機 Git Bash 經離線 runner 二次啟動 Windows Store Python 時，兩項既有 CreateProcess 測試遇到 WinError 87；單項直接執行可通過，單純重導 stdin 仍無法修復 wrapper。driver 命令改為直接執行同一套完整 unittest、五個 Node 檢查及 diff 檢查，確切命令保存在 [.issue-loop-evidence.json](../.issue-loop-evidence.json)。既有程序控制邏輯維持原樣。

CI 在既有 Linux／Windows matrix 加裝選用 Web 依賴，避免 HTTP tests 因缺依賴而略過。此次沒有 push，尚未取得 CI 跨平台收據。此專案原先沒有 coverage ratchet、CRAP 或 mutation gate；本次未為該 Issue 引入整套工具。新增 Python 檔另經 Ruff 檢查。

## 效能與瀏覽器紀錄

2026-10-17（Asia/Taipei）先記 ungated baseline，再設定路由 p95 ≤ 1,000 ms、8 併發 p95 ≤ 2,000 ms、peak RSS ≤ 256 MiB 並重跑。Windows build 26300／Python 3.11.9／SQLite 3.45.1／20 logical CPUs／約 63.6 GiB RAM。

| 規模 | baseline 最慢路由 p95 | 重跑最慢路由 p95 | 重跑 8 併發 p95 | 重跑 peak RSS |
|---|---:|---:|---:|---:|
| 1k 影片 | 56.644 ms | 55.400 ms | 113.539 ms | 73.516 MiB |
| 10k 影片 | 239.887 ms | 493.663 ms | 300.527 ms | 84.590 MiB |

原始 [baseline 與 verification](dashboard-benchmark.json) 保留各路由樣本與機器資訊；每輪 374 個 app GET，10k 使用 100,290 筆 FTS events、10k×384D 合成向量與 300 筆長證據。這是暖快取 ASGI 合成資料測試，未量測真實向量模型或瀏覽器 CWV／TCP／TLS，不代表所有硬體的效能保證。

Chromium headed browser，隔離的 107 支影片 fixture、假 provider、禁止 outbound provider transport：Library 第 2 頁 50 筆、證據第 1 頁 50 筆；無證據 Ask 回 insufficient_evidence；工作頁可見 blocked／unknown 與不退款提醒；Digest 預覽確認後顯示供應商接受，明確新版再送出保留兩筆通知。修正 async 按鈕事件後再次驗證，主控台 0 errors／0 warnings。360px 詳情與 320px 摘要頁 scrollWidth 均小於 viewport，沒有水平溢出；截圖保存在執行端 `output/playwright/`。

本次未使用真實付費 API、SMTP／Resend 寄信、真實收件者、外部部署或實體手機。Windows 不允許建立 symlink 的安全案例及既有 POSIX 專屬測試會明確 skipped，未宣稱已驗證。介面不會虛構遠端 delivery／bounce／人工處置或未知費用結果。
