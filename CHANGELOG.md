# Changelog

## Unreleased

- 新增本機 Web Dashboard（`pipeline.py --serve`）：唯讀為主的影片情報介面——總覽統計（依 `eligible_at` 的新片計數、RSS 狀態、成本/評分摘要，缺資料顯示 unavailable）、影片庫分頁與證據詳情、本機 FTS 搜尋、operations/失敗/供應商監控、摘要與通知歷史。純標準函式庫實作，既有 CLI 不受影響。
- 副作用一律走 `operations.db` 的 `operation_tasks` 持久化佇列（`--serve-worker` 執行）：語意搜尋、Ask、RSS 重排、摘要寄送經預算閘與 idempotency 去重後回 202+task_id；claim/lease/fencing、heartbeat、retry backoff、cancel 與 crash recovery（遠端已送出的標 unknown 不盲目重做）。
- Dashboard 安全基線：loopback 預設、Host allowlist、Origin/CSRF 檢查、錯誤遮罩、HTML autoescape、URL scheme allowlist、body/rate/分頁/排序/查詢長度上限、讀取走唯讀連線且不做 schema 遷移；非 loopback 需 allow-remote＋token＋trusted-proxy 三者齊備並強制 Bearer。

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
