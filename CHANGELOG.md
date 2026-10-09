# Changelog

## Unreleased

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
