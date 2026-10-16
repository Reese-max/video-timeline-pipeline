# Changelog

## Unreleased

- PR #40 自我審查修正：每個實際傳輸重試各自預留與入帳，Apify／Notebook 建立請求不自動重送；Notebook 替換與 CLI 通知先套用預算。預留查核以寫入交易序列化，未結清風險跨日保留並透過新增的帳本關聯欄位避免重複計算；保留舊資料。修復 Apify JSON 落盤、MiniMax 回傳 token 用量、Bright Data 未確定回應與尚未送出之本機失敗記帳，以及內建自測、Windows 測試環境與 SQLite 連線清理。
- Instagram 盤點先載入專案／root 環境設定並驗證正整數結果上限與正數逾時；stderr 在提交前顯示有效上限。非物件採集回應保留未確定回執與成本預留，不以重送恢復；離線 HTTP／檔案／SQLite 回歸不代表真實供應商計費驗收。
- Bright Data Instagram 作者盤點需要 finite 結果預算；未提供 `--instagram-max-items` 時需設定 `BRIGHTDATA_INSTAGRAM_MAX_ITEMS`，避免 `--allow-batch-cloud` 搭配無上限的供應商請求。
- 整合既有背景佇列、簽名媒體網址與抽樣更新；停用整工作目錄刪除，清理與同步／背景操作共用鎖，保護佇列失敗、使用中及分享租約，保留新式分析路徑的文字結果與回執。
- 新增 MCP 影音暫存 7 天清理指令（預設 dry-run），只移除完成且不需覆核工作的已知影音產物，保留摘要、逐字稿、回執與 Drive；沿用既有 OS 鎖阻擋清理／MCP 競態，媒體清除後仍可重新下載。
- 成本守衛接到每一條已啟用的付費傳輸（#4）：Groq 轉錄、MiniMax 視覺／摘要／關鍵詞／問答／digest／分類、OpenRouter embeddings、Meta Graph 分頁、Apify Actor 啟動、Bright Data trigger、Open Notebook 同步、RSS 通知 Webhook 與 Resend／SMTP／Webhook 寄送。送出前原子預留、超額時不開連線、送出後入帳；超時或結果不明不結清預留。嚴格模式（`BUDGET_HARD_STOP=true` 且 `DAILY_BUDGET_USD>0`）只放行帶供應商端可執行上限的操作：目前僅 Apify Actor run 傳入 `maxTotalChargeUsd`；Groq、MiniMax、OpenRouter 等估算不冒充硬上限而會被拒絕；未開嚴格模式時守衛只記錄估算、不保證不超出供應商費用。

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
