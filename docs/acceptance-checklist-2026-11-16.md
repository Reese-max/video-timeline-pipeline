# 真實環境驗收清單（2026-11-16）

基準：`main@73480c8`。下列 issue 的程式修正與離線回歸都已在 main（依據見 [issue-sweep-2026-10-24.md](issue-sweep-2026-10-24.md) 與各 issue 留言），開著只差**真實媒體／服務／ChatGPT host** 的驗收。離線 CI、mock host、fake provider 都不能代替這些步驟。

標示：💰 會呼叫付費 API；👤 需要人在 ChatGPT／Drive 介面操作；🔍 需要人工判讀。

## 共同前置

1. 本機依賴：`.\.venv\Scripts\python -X utf8 -m pip install -r requirements.txt`（Windows 繁中環境需 `-X utf8`）。
2. 離線基線先跑過：`python -X utf8 scripts/check_offline.py`，有失敗先處理，不進入下列驗收。
3. #55／#56 需要原始 166.6 秒影片（舊 job `16fae7864ca8`）。2026-10-26 在 Drive 只讀搜尋找不到可確認的來源媒體，**需要原始影片網址或檔案**；找不到時改選一支 2–3 分鐘、畫面有字幕且有連續語音的影片，並在結果註明「替代媒體」。

## 一、CLI 即可驗收（不需 ChatGPT）

### #55 Whisper 切段邊界漏轉 💰🔍
1. 對原始影片重新處理（不要沿用舊快取）：`python pipeline.py --url '<原始網址>'` 或 `--video '<檔案路徑>'`。
2. 打開產出的逐字稿 JSON，確認：
   - `gap_check.remaining` 為 `[]`、`backfill_errors` 為 `[]`。
   - 除片頭片尾外，相鄰句子之間沒有超過 5 秒的空白。
   - 2:00–2:28 有對應語句（舊案例的辨識片語：「所有上传的资料都能在这里找到」）。
3. 通過：在 issue 貼上 job ID、`gap_check` 欄位與 2:00–2:28 的逐字稿片段後關閉。

### #56 MiniMax 畫面時間碼壓縮 💰🔍
1. 用 #55 同一次處理的結果（`type=visual` 的畫面證據）。
2. 確認時間碼分布在 0–166.6 秒，不再集中於 0–6.9 秒；結果沒有 `finish_reason=timestamp_coverage` 或 `length` 的未完成標示。
3. 至少抽 5 筆，用播放器跳到該時間點截圖，與證據描述比對，誤差 ≤ 5 秒。
4. 通過：在 issue 貼上時間碼分布、抽樣截圖與誤差表後關閉。

### #5 Ask My Videos（Hybrid Search + RAG）🔍💰
1. 人工挑 10–25 題，每題標出正確影片與可接受的時間範圍（golden evidence）。
2. 同一組題目分別跑 baseline 與 hybrid：
   `python pipeline.py --ask '<題目>' --json` 與 `python pipeline.py --ask '<題目>' --hybrid --search-strict --json`。
3. 逐題記錄：引用是否落在 golden 範圍、是否誤判 `insufficient_evidence`、token 與耗時。
4. 結果補進 [issue-5-retrieval.md](issue-5-retrieval.md)「已知界線」一節；hybrid 不劣於 baseline 即可關閉 #5 的檢索部分。

## 二、需要 ChatGPT host 與 Google Drive 👤

前置：MCP 服務以最新 main 啟動，ChatGPT Developer Mode 中**重新整理外掛動作**，在新對話產生新 widget（舊訊息裡的 widget 不會更新）。

### #57 A／B 並行
1. 在新對話用一支影片網址觸發 prepare。
2. 記下分析（A）與 Drive 上傳的開始時間（widget 狀態或伺服器 log），兩者啟動差 < 10 秒。
3. A 還在跑時，Drive 完成後 widget 就要顯示交接內容（不含 A 的結果），「複製並開新一般對話」按鈕可用（原需求的兩顆按鈕已合併為一顆，見 CHANGELOG）。

### #58 A_server_analysis.md 進 Drive
1. 延續 #57 那次工作，等 A 完成。
2. 到同一個 Drive 資料夾確認有 `<片名> - <job id> - A_server_analysis.md`，內容含逐字稿與畫面時間軸。
3. widget 顯示「A 已完成」，「複製合併指令」可用，貼出的文字以「合併：」開頭並含 file_id 與 SHA-256。

### #23 檔案引用交接
1. 在支援 `uploadFile` 的 ChatGPT host 觸發 widget 上傳。
2. 確認 ingestion tool 實際收到至少一個 `file_id` 與 `download_url`（看 MCP 伺服器 log）。
3. 要求 ChatGPT 描述某一代表影格中看得到的具體內容（字幕文字、物件），人工確認正確。
4. 故意讓交接失敗（例如在不支援 `uploadFile` 的 host 操作），確認不會產生只靠 metadata 的影片摘要。

### #59 uploadFile library 實驗
1. 在支援 `uploadFile` 的 host 展開 widget 的「實驗：存到 ChatGPT 檔案庫（#59）」，點一個分段按鈕存入 MP4。
2. 核對回傳的 file_id 與 SHA-256 與本機分段一致；請 ChatGPT 描述指定時間點畫面並人工確認。
3. host 不支援時，widget 必須顯示不支援，而不是假裝成功。這種情況也算實驗有結論，記錄後可關閉。

## 不在本清單

- #7 Roadmap、#10 研究型：需要真實影片集、人工標註與付費執行，另案處理。
- #60 ChatGPT 外掛清單殘留：平台端限制，不是 repo 程式問題。
