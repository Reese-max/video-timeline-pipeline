# Changelog

## Unreleased

- 改進：影片分段預設改成「直接剪開」（`MCP_SEGMENT_MODE=copy`，stream copy），不再整支重新編碼；copy 失敗（ffmpeg 失敗或分段沒通過既有驗證：列數、檔名、時間連續、最後一段接近影片長度）時，同一次呼叫內自動清掉半成品並退回重新編碼，stdout 印出一行繁中原因；取消與逾時照常往上拋，不退回。A1 主機（2 OCPU）實測 25.5 分鐘 VP9 1080x1920：重新編碼（veryfast crf 28）約 14–15 分鐘，stream copy 只要 1.1 秒（13 段、邊界在 120 秒倍數，VP9 分段可正常產生預覽圖）；若必須重新編碼，長邊縮到 1280 為 2.63 倍速、檔案小 39%（現行 1080p 為 1.82 倍速），`ultrafast` 沒有更快且檔案大 3 倍。新增 `MCP_SEGMENT_MAX_LONG_SIDE`（預設 1280，0 = 不縮放；非 0 時必須 ≥ 240 且為偶數，只縮小不放大、維持比例、寬高皆偶數，直式／橫式與手機影片旋轉 metadata 都正確）與 `MCP_SEGMENT_X264_PRESET`（預設 `veryfast`，只接受 `superfast`／`veryfast`／`faster`／`fast`／`medium`）；非法環境變數直接報錯且不呼叫 ffmpeg。copy 模式不做續跑（重切只要幾秒），transcode 模式保留中斷續切；模式或設定改變時舊分段全部重切（記在 `segments/.split-params.json`）。分段可能維持原編碼（例如 VP9，部分瀏覽器／iOS 無法播放）；需要到處都能播放的 H.264 分段請設 `MCP_SEGMENT_MODE=transcode`。

- 新增：Bright Data Facebook `--facebook-brightdata-mode {auto,reels,posts}` 與訂閱／OPML 模式保存；Reels（`gd_lyclm3ey2q6rww027t`）在 auto 且全為 `dead_page` 時，當次切換 Pages Posts（`gd_lkaxegm826bjpoo9m5`），既有訂閱刷新會在送出前先保存 posts 模式，即使後續失敗也不再先試 Reels；新增（含 OPML 匯入）僅於整次成功時建立訂閱，失敗不留半成品，重試沿用回執。兩次 trigger 獨立預留／結算、分開回執；只有送出前額度拒絕可依既有授權走 Apify 備援。
- 新增：posts 本機篩選影片及日期，使用 Facebook 貼文／影片網址而非 CDN；非影片貼文同樣計費。成功保存後以 `facebook_seen_post_ids` 記住含非影片的有效貼文 ID，去重保留最近 500 筆，下次與影片 ID 一起排除，減少重複計費。

- 修正：Facebook／Instagram Bright Data 作者盤點先結算含錯誤的全部 record，再以 `error_count`／`errors` 回報；全部錯誤時 CLI 非零結束、RSS 記錄失敗且不推進 snapshot，部分錯誤保留正常項目，重試沿用回執不重送計費 trigger。

- 新增：Facebook 粉專 Reels 可用 `--facebook-provider brightdata --facebook-max-items N`，結果預算支援訂閱更新與 OPML；與 IG 共用單次提交／snapshot 續查、本機日期篩選及 RSS 付費盤點間隔。`RSS_FACEBOOK_FALLBACK_PROVIDER=apify` 僅在帳本送出前拒絕且 Apify 另獲放行時備援，送出後失敗不另開計費工作。
- 新增：`BRIGHTDATA_MONTHLY_CREDIT_LIMIT`（4500）、`BRIGHTDATA_CREDIT_LEDGER`（預設使用者共用 SQLite）、`BRIGHTDATA_POST_DAILY_LIMIT`（20），原子預留最壞結果筆數、依實際交付含 error records 結算，未知結果保留預留；`--brightdata-usage` 輸出月／日用量 JSON。免費層每月 5,000 credits，由 Scraper／SERP／Unlocker／Browser 共用，每月 1 日 UTC 重置且不累積，無儲值用完即停止。
- 新增：`BRIGHTDATA_ALLOWED_POST_URLS=*` 允許既有驗證範圍內任何公開單篇貼文，仍需 `allow_paid=true`；共用月額度或每日上限擋下時回 `quota_exhausted`／`BRIGHTDATA_QUOTA` 與 `resets_at`，到期前禁止重試或換網址繞過。`RSS_PAID_DISCOVERY=brightdata` 可放行 FB／IG 排程刷新。
- 新增：`RSS_PAID_DISCOVERY_MIN_HOURS` 限制排程自行放行的付費盤點（如 Apify 粉專）每個訂閱的最短間隔；未到間隔記為 `deferred`，明確 `--allow-batch-cloud` 不受限。
- 修正：Apify 粉專盤點成功後寫快照時因 `max_total_charge_usd` 是 Decimal 而崩潰（10-21 A1 實測：run 已計費、結果卻沒存下）；改存字串並加回歸測試。
- 新增：`RSS_PAID_DISCOVERY`（如 `apify`）讓排程刷新只對列出的付費盤點供應商自行放行，其他供應商仍須 `--allow-batch-cloud`。
- 新增：RSS 處理完成的影片把 `knowledge.md` 存到 Drive「RSS 自動摘要」資料夾（ID 先存檔、同內容沿用、不覆寫被改動的檔案；Drive 失敗只記錄不讓影片失敗；`RSS_DRIVE_UPLOAD=false` 可關閉）；完成通知加上前 300 字摘要與 Drive 連結。
- 介面：v17 widget 視覺更新（卡片式版面、主次按鈕、狀態與缺漏提示框、時間碼標籤、逐字稿／畫面觀察分色、跟隨 ChatGPT 淺色／深色主題）；Drive 交接改一顆「複製並開新一般對話」（先複製再嘗試開分頁並顯示連結備援，複製失敗不開分頁並顯示手動複製與連結，保留「只複製」）；其餘 id、文字與行為不變，UI URI 升為 v17 並保留 v15、v16。另修正報告區兩個簡體字（独立、实际）。
- 改進 A／B 韌性（依 job c9289f71a482 實測：MiniMax 連續 4 次 HTTP 529）：語音可靠、畫面失敗的 partial 報告標 `evidence_scope=speech_only` 並免摘要自動上傳 Drive（報告開頭註明只含語音）；快照對所有可自動上傳的報告回 `awaiting_segments`，widget v16 自動追蹤、提示建議加跑 B（`quality.route_b_reasons`）、複製改三段備援；保留 v14／v15 widget URI 避免升版後「無法開啟應用程式」；HTTP 429／529 拒絕記 `zero_rejected` 不佔預算；MiniMax 同一影片 24 小時內重跑重用 `file_id`。

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
