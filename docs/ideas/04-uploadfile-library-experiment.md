# 04｜實驗：用 `window.openai.uploadFile` 把影片存進 ChatGPT 檔案庫

> 狀態（2026-10-24）：小工具端按鈕、片段時間換算與離線契約測試已整合；媒體分支 v19 先取得並核對 ingestion 回執再公開模型 ID。真實 ChatGPT 檔案庫及模型讀取仍未實測。`window.openai.uploadFile` 是 ChatGPT 宿主提供給小工具的用戶端 API。

## 1. 目的

不經過 Google Drive，讓 ChatGPT 直接讀到伺服器上的 MP4。如果成功，甚至可能不用另開一般對話。

## 2. 官方文件與社群查證（2026-10-10）

| 關鍵點 | 結果 | 來源 |
|---|---|---|
| `uploadFile` 支援非圖片檔 | ✅ 2026-03-09 更新「now supports non-image file types」 | Plugin UI changelog |
| `{ library: true }` 存進使用者的 ChatGPT 檔案庫 | ✅ 2026-03-24 新增；但「when that library is available to the current user」，不是所有帳號都有 | changelog、Reference |
| 小工具能不能自動抓伺服器的檔案再上傳 | ❓ 文件寫的是「Upload a user-selected file」，沒說程式產生的 File 可不可以 | Reference |
| 檔案大小上限 | ❓ 文件沒寫（segment_000 有 13.2 MB） | Reference |
| 上傳後模型在同一個對話看不看得到 | ⚠️ 社群回報用 `imageIds` 讓模型看圖失敗，OpenAI 2026-02-22 說會查，之後沒有修好的消息 | 社群討論串 |
| `getFileDownloadUrl({fileId})` | ✅ 可以拿到暫時的下載網址，適用於小工具上傳的檔案 | Reference |

**推論：** 最可能的用法是「小工具把 MP4 存進檔案庫 → 使用者在對話裡從檔案庫把影片附加進來 → ChatGPT 讀取」。模型應該不會自動看到小工具上傳的檔案。

## 3. 測試按鈕（現有 Video Timeline 小工具）

實作集中在 [web/widget.html](../../web/widget.html) 的 `saveSegmentToLibrary` 與 `askAboutLibraryFile`。[官方 Reference](https://developers.openai.com/plugins/reference#file-apis) 確認 `uploadFile(file, { library: true })` 及 `getFileDownloadUrl({ fileId })` 的介面；檔案庫依帳號提供，API 回傳成功不代表模型已讀取。

| 驗收要求 | 實作／證據 |
|---|---|
| 每個片段明確點擊後才上傳 | 每列「存到 ChatGPT 檔案庫」按鈕；能力偵測及離線無點擊測試 |
| 成功／錯誤診斷 | fileId、實際下載大小、耗時、SHA-256、下載網址是否可用；錯誤保留說明並遮蔽簽章網址 |
| library 不可用時的備援 | 額外點擊「改用一般上傳」，保留兩次結果；刻意不採原提案的自動重送，避免不明結果造成重複檔案 |
| 同對話讀取實驗 | 另按「請模型讀這個檔案」，先保存 `modelContent.uploaded_file_ids`，再送出 MCP Apps 訊息；宿主不支援標準 bridge 時使用 `sendFollowUpMessage` |
| 指定時間點 | 按上傳時的片段起訖換算原片 0:30、1:05、2:10；0–120 秒片段問第 30／65 秒，120 秒起的片段問第 10 秒；不把標準答案或預期雜湊送給模型 |
| 其他片段及重新整理 | 僅選落在片段內的時間點；沒有指定點時選片段內最多第 10 秒的時間，範圍未知則明說；重新整理不改變已上傳片段的時間依據 |
| 真實檔案庫／模型可讀取 | **未驗證**；離線測試不等於檔案庫出現、上傳後 bytes 相同或模型實際解碼 |

`node tests/test_widget_library_experiment.cjs` 執行實際 widget script 與假的宿主／MP4 bytes；涵蓋 SHA-256、時間換算、備援、錯誤遮蔽及狀態競態，不呼叫 Drive 或付費模型。完整實測清單見 [widget_manual_checks.md](../../tests/widget_manual_checks.md#59-chatgpt-檔案庫實驗)。

**要求：**
- 每個片段一個按鈕；成功時在畫面上顯示 `fileId`、大小、耗時，失敗時顯示完整錯誤訊息（不要吞掉）。
- 先測 segment_001.mp4（4.4 MB），再測 segment_000.mp4（13.2 MB）。
- 另外加一個按鈕：用 `setWidgetState({ modelContent: { uploaded_file_ids: [...] } })` 加上 `sendFollowUpMessage`，請模型讀這個檔案，測試同一個對話裡模型看不看得到。

## 4. 驗證步驟

1. 按按鈕 → 確認回傳 `fileId`，檔案也出現在 ChatGPT 檔案庫。
2. 在掛外掛的同一個對話，從檔案庫附加這支影片，問驗證題；不行的話再改到一般對話試。
3. 驗證題和標準答案：
   - segment_000 的 SHA-256 → `53572b408974589c7fded269dcbacbe38f71d2cb8944c92083a8f109fadb26b4`
   - segment_001 的 SHA-256 → `bfe01467050db6569ea7664debf13ee85613262aa10cbb7d1605b1a300642763`
   - 0:30 → 上傳資料頁「个人健康档案 PRIVATE WORKBENCH」，右側三步：私密保存原件／视觉模型识别／人工核对
   - 1:05 → CareArk 視窗「正在本机运行：http://127.0.0.1:58320/」
   - 2:10（segment_001 第 10 秒）→ 「我／妈／+新增成员」選單，¥1522.40，记录医院 7
4. 記錄：每一步成功或失敗、錯誤訊息、耗時、檔案大小上限（如果碰到的話）。

## 5. 這次沒能實測的原因（2026-10-10）

- 小工具的程式在使用者的伺服器上，Claude 改不了。
- 想在 Claude 的雲端環境另架測試伺服器，但環境的安全政策不允許開對外通道。
- Chrome 擴充功能連線失敗，沒辦法操作 ChatGPT。

本次 worker 僅修改及測試工作樹，未部署或操作真實 ChatGPT 工作階段。部署由 driver 處理後，依第 4 節驗證並分別記錄檔案庫與模型讀取結果。

## 來源

- <https://developers.openai.com/plugins/changelog>
- <https://developers.openai.com/plugins/reference>
- <https://community.openai.com/t/window-openai-uploadfile-and-setwidgetstate-and-imageids/1372230>
- <https://community.openai.com/t/please-make-window-openai-uploadfile-file-support-csv-type/1370003>
