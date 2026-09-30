# Changelog

## Unreleased

- 修正 PR #21 審查缺陷：向量重建原子替換與失敗回復、供應商及快取驗證、FTS 摘錄、有界向量候選、context 預算與單片證據保留、引用拒答、離線旗標及日期中繼資料；新增離線回歸測試。
- 新增 `occurrences` 證據層：每筆片段有重建不變的 `occurrence_id` 與 `timestamp_kind`（source_level／point／range），引用不再依賴會重排的 FTS rowid。
- 新增 `--hybrid` 混合檢索：篩選 eligible-set 後全文＋向量候選去重、RRF 融合，語意路不可用時明確降級（`--strict-retrieval` 可改為失敗）。
- 新增檢索篩選：`--kind`、`--job`、`--platform`、`--author`、`--since/--until`、`--date-field`、`--include-unknown-dates`；分類改為 JSON 精確比對，日期未知預設排除並提示筆數。
- `--ask` 改為證據導向：模型僅回引用編號，非法編號剔除計數、注入文字視為不可信資料、查無證據回傳 `insufficient_evidence`。
- 向量以 `backend::model` 契約命名；新增 `--embed-plan`／`--rebuild-embeddings`、回應筆數與維度／有限值驗證，契約不符拒絕比較。
- 新增 `--json`／`--explain` 輸出與唯讀檢索連線；`--audit` 擴充 occurrences 一致性與未知發布日統計。
- 修正 README 過時的相似度門檻與 OpenRouter 文件說明。
- 新增 `tests/test_retrieval.py` 與可重放的 `evals/ask_eval.json` 評估集（26 案）。

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
