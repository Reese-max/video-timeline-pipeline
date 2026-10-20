# Changelog

## Unreleased

- 改進 A／B 工作流（依 job e47517f85346 實測）：分析證據完整時，A 報告不必等 ChatGPT 摘要即自動上傳 Drive；B 交接內容改為一次貼上（先獨立輸出，再讀同資料夾 A 報告合併）；合併規則改為畫面文字採 B、A 不同讀法放附錄、語音採 A；逐字稿只有低信心片尾套語（如「ご視聴ありがとうございました」，avg_logprob < -0.3）、只有符號（♪）或沒有語音時，`quality` 標 `speech_reliable=false`、`recommend_route_b=true`。只有證據完整且 `quality.status=evidence_available` 才免摘要上傳；交接內容要求 B 先核對報告 job_id。widget 未改動（報告排程後需手動重新取得狀態）。

- 修正 #58：v15 widget 在摘要已提交但 Drive 片段／worker 尚未完成（`awaiting_segments`）時，以唯讀快照追蹤至報告排程，再接續既有上傳工作輪詢。查詢核對 job、cache signature、分析與報告雜湊；最多 60 次，失敗或逾時停止並提示手動刷新，不重送摘要、分析或上傳。新增提交／通知／恢復、錯誤與綁定不符、等待上限及換影片／關閉的回歸測試；真實 Drive 與 ChatGPT 驗收仍待執行。

- 新增 #59：v14 widget 提供可收合的 ChatGPT 檔案庫實驗，每個分段須明確點擊才 fetch／uploadFile；一般上傳亦須另行點擊。顯示上傳資訊、SHA-256 與完整遮蔽網址的錯誤；隔離連點、換 job 及實驗狀態保存失敗，模型讀取要求走既有訊息橋接與統一保存。新增獨立 Node 測試並納入離線檢查，固定既有 CSP／成功媒體 CORS 前提；檔案庫與模型可讀性仍需真實 ChatGPT 驗證。已知限制：上傳卡住時需重新載入介面。

- 補強 #58：v14 widget 在 A 分析完成後自動讀取報告，明示摘要提交前尚未上傳；提交摘要後以 1–10 秒退避輪詢報告工作，驗證完成後顯示「A 已完成」並啟用合併複製。失敗保留完整錯誤，換 job／關閉停止輪詢。合併指令採 §4.3 原文，以「合併：」開頭並保留 file_id／SHA-256／未獨立驗證聲明；README 說明 paired worker 與 caller_submitted 上傳條件。UI URI 同步升為 `ui://video-timeline-pipeline/v14-<媒體網址 hash>.html`。

- 修正 #56 follow-up B：CLI 搜尋／問答／章節與知識檔章節／待查證資訊不再提供不可靠畫面時間碼的跳轉；本機索引簽章升至 2，以 NULL 保存不可信時間並排除 `timestamp_*` 診斷文字，保留畫面內容且不更動摘要快取。

- 新增 #57（A／B 並行 §4.1–§4.2）：prepare 完成後 widget 自動同時啟動 `analyze_prepared_video`（A）與 `upload_prepared_video_to_drive`（B，Drive 先送、互不等待、失敗隔離；未設定 Drive 時只跑分析）。Drive 完成即顯示交接內容，只放檔案 ID／網址／大小與原片分段時間，不含任何 A 的結果，並提供「一鍵複製」「開新一般對話」按鈕。伺服器在 Drive 結果與 `get_prepared_video` 附上同一份 `handoff_text`（B 固定輸出格式：一句話重點、分段表、限制說明；結尾提醒收到「合併」前不要讀取 A，#58 實作 A 檔案）。paired worker 允許同一 job 的分析與 Drive 片段上傳並行，最多兩個 worker；widget 雙軌追蹤兩路工作。離線驗證 `python3 -m pytest -q` 與 `node tests/test_widget_handoff.cjs`（39 項）皆通過。

- 修正 #56：`video_mp4` 畫面證據的時間碼不再被壓縮在前幾秒。提示詞明定 timestamp 是從 0 起算的原片秒數且事件須覆蓋全片到片尾；解析接受純數字、`MM:SS`／中文時間，無法解析的事件直接丟棄（不再靜默記為 0.0）；事件峰值不到 `min(片長-30 秒, 片長一半)` 或超出片長 30 秒以上時沿用已上傳檔案 ID 只重問一次，仍對不上就以 `finish_reason=timestamp_coverage` 標示未完成、不寫入可重用快取。短片（單一事件即達豁免門檻）不受影響，仍只呼叫一次。

- 修正 #52：`video_mp4` 視覺分析不再以裸子字串 `exceed` 誤判檔案過大。`is_minimax_video_size_error` 排除 HTTP 429、`rate limit exceeded`、`exceeds daily limit`、預算阻擋及配額等非大小錯誤，並讓限流與預算超限錯誤直接冒泡，避免多餘壓縮與重複上傳重試。

- 成本守衛接到每一條已啟用的付費傳輸（#4）：Groq 轉錄、MiniMax 視覺／摘要／關鍵詞／問答／digest／分類、OpenRouter embeddings、Meta Graph 分頁、Apify Actor 啟動、Bright Data trigger、Open Notebook 同步、RSS 通知 Webhook 與 Resend／SMTP／Webhook 寄送。送出前原子預留、超額時不開連線、送出後入帳；超時或結果不明不結清預留。嚴格模式（`BUDGET_HARD_STOP=true` 且 `DAILY_BUDGET_USD>0`）只放行帶供應商端可執行上限的操作：目前僅 Apify Actor run 傳入 `maxTotalChargeUsd`；Groq、MiniMax、OpenRouter 等估算不冒充硬上限而會被拒絕；未開嚴格模式時守衛只記錄估算、不保證不超出供應商費用。

- 修正 #18：預算期間改以 `COST_TIMEZONE`（IANA 時區，預設 Asia/Taipei）的本地日／月為界；無效時區明確失敗而非退回 UTC。預算狀態與成本報告輸出 `accounting_timezone` 供稽核；新增凍結時鐘測試涵蓋台北跨 UTC 日界、本地午夜 rollover 與 DST 時區。無效時區在開啟／初始化 operations.db 之前就明確失敗，不留下已遷移的資料庫。未結清的預算保留與已入帳費用改用同一段 UTC 時間窗過濾，因此變更 `COST_TIMEZONE` 時未結清金額不會脫離當期預算；新增 `(account_scope, created_at, status)` 索引維持該查詢走索引。無效時區時 `--budget-status`／`--cost-report` 以一行訊息與 exit 1 回報，不再丟出 traceback。離線驗證 `python3 -m pytest -q`：140 項通過（另 88 個 subtests）；`python3 -m compileall -q .` 通過，皆 exit 0。全程僅用凍結時鐘與本機 SQLite，未呼叫任何供應商或網路。`requirements.txt` 新增 `tzdata`，供沒有系統 IANA 資料庫的環境（如 Windows）讓 `zoneinfo` 正常運作。
- 整合既有背景佇列、簽名媒體網址與抽樣更新；停用整工作目錄刪除，清理與同步／背景操作共用鎖，保護佇列失敗、使用中及分享租約，保留新式分析路徑的文字結果與回執。
- 新增 MCP 影音暫存 7 天清理指令（預設 dry-run），只移除完成且不需覆核工作的已知影音產物，保留摘要、逐字稿、回執與 Drive；沿用既有 OS 鎖阻擋清理／MCP 競態，媒體清除後仍可重新下載。
- MiniMax 僅接受完整的 `finish_reason=stop`；截斷或欄位不完整的畫面觀察保留未完成狀態，不寫入可重用快取。逐張與整批視覺快取均檢查 schema，知識檔與 manifest 明確標示視覺完成狀態；以假回應驗證，真實 MiniMax 尚待實測。
- 2026-09-24：統一 Bright Data 單篇與 IG 作者採集的單次提交／持久回執；未知回應禁止重送，已知 snapshot 可續跑，作者下一輪以成功落盤的 snapshot 推進。基於 `703e649`，本批尚未推送或部署；離線驗證 `python -X utf8 -m unittest discover -s tests -q`：116 項、114 通過／2 項平台略過；`node tests/test_widget_handoff.cjs`：21 項通過；`git diff --check` 通過，皆 exit 0。下一驗收為審閱、推送與部署後的非新增計費 smoke test；不沿用先前部署的線上驗收。
- 新增 ChatGPT MP4 檔案引用交接工具 `ingest_video_segments`：驗證 `download_url`／`file_id`、暴露完整 `_meta["openai/fileParams"]` 與 `outputSchema`，未完成交接時不啟動摘要。
- 新增 Facebook Page 作者盤點與追蹤，透過 Meta Graph API 分頁取得影片及 Reels，再沿用既有 RSS 佇列與影音摘要管線。
- 新增可選 Apify 公開 Page 發現器，含 Bearer Token、Actor 費用上限、非同步輪詢、資料集分頁與影片貼文過濾。
- 新增 Bright Data 公開 Instagram 作者 Reel 盤點與訂閱，沿用既有去重、重試及 Groq→MiniMax 處理流程。

## 0.2.0 — 2026-09-03

- 新增 Groq 時間戳轉錄與 MiniMax 繁中摘要、畫面理解。
- 新增 RSS／Atom／Podcast 收件匣、快取、續跑與失敗重試。
- 預設 Groq／摘要 workers 4、MiniMax 畫面 workers 8。
- 新增自動抽幀與穩定畫面快取。
- VidBee sidecar 同時輸出字幕與相鄰筆記檔。

## 0.1.0

- 建立本機影片時間軸、字幕、筆記與搜尋索引管線。
