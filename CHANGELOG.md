# Changelog

## Unreleased

- 將既有 PR #37 成本守衛整合到 retrieval 候選，保留目前 main 的佇列／媒體安全；新增 rerank 邊界守衛，query embedding 或 rerank 遭預算拒絕時不吞掉錯誤或嘗試另一個 provider。完整 suite 以假傳輸驗證，真 API／部署驗收未執行。

- Ask 回應改用完整來源原文的嚴格 JSON 契約：保留合併前片段邊界並綁定 occurrence IDs，程式呈現來源類型；改寫、截去日期／否定詞、非法引用或模型來源 URL 使回答拒絕。合法引用不再自動表示主張受支持，`semantic_support_verified` 明確維持 false；不增加 provider 呼叫。A5 語意評估與 A9 人工 golden dataset 仍待完成。
- 複核修正 r3：hybrid 降級接住所有嵌入錯誤型別、audit 向量覆蓋改按不重複文字計且忽略 stale 向量、`--search-limit`／`--ask-limit` 驗證 ≥ 1、JSON `retrieval_mode` 固定機器 token（人類描述移至 `mode_label`）、`--min-similarity` 延伸到 hybrid 向量腿、合併引用的 end 取最大範圍、RRF 缺 occurrence_id 時改內容 hash 去重、向量候選按 hash 去重搬 blob、`--explain` 支援 `--ask`、`--ask` 空字串明確報錯；新增對應回歸測試。
- 修正 PR #21 審查缺陷：向量重建原子替換與失敗回復、供應商及快取驗證、FTS 摘錄、有界向量候選、context 預算與單片證據保留、引用拒答、離線旗標及日期中繼資料；新增離線回歸測試。
- 新增 `occurrences` 證據層：每筆片段有重建不變的 `occurrence_id` 與 `timestamp_kind`（source_level／point／range），引用不再依賴會重排的 FTS rowid。
- 新增 `--hybrid` 混合檢索：篩選 eligible-set 後全文＋向量候選去重、RRF 融合，語意路不可用時明確降級（`--strict-retrieval` 可改為失敗）。
- 新增檢索篩選：`--kind`、`--job`、`--platform`、`--author`、`--since/--until`、`--date-field`、`--include-unknown-dates`；分類改為 JSON 精確比對，日期未知預設排除並提示筆數。
- `--ask` 改為證據導向：模型僅回引用編號，非法編號剔除計數、注入文字視為不可信資料、查無證據回傳 `insufficient_evidence`。
- 向量以 `backend::model` 契約命名；新增 `--embed-plan`／`--rebuild-embeddings`、回應筆數與維度／有限值驗證，契約不符拒絕比較。
- 新增 `--json`／`--explain` 輸出與唯讀檢索連線；`--audit` 擴充 occurrences 一致性與未知發布日統計。
- 修正 README 過時的相似度門檻與 OpenRouter 文件說明。
- 新增 `tests/test_retrieval.py` 與可重放的 `evals/ask_eval.json` 評估集（26 案）。
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
