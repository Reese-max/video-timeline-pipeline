# 01｜測試結果

## 1. ChatGPT 外掛設定變更（2026-10-08）

- **原因：** Funnel 主機名從 `cursor-grokbot-1／-2／-3` 改成 `cursor-grokbot`，舊網址全部解析不到（curl 回 000）。
- **新增：**「Video Timeline Pipeline - cursor-grokbot」
  - 網址：`https://cursor-grokbot.tail930e77.ts.net/video-timeline/mcp`
  - 驗證方式：無需身分驗證
  - 狀態：已連線
- **刪除：**「Video Timeline Pipeline - Cursor」（舊網址 grokbot-1）和「Video Timeline Pipeline - Cursor Connection」（舊網址 grokbot-2）。
- **刪不掉：**「Video Timeline Pipeline - Cursor Fixed」（Workspace upload，包著已刪除的 grokbot-2 App）。
  - 選單只有「編輯／下載 ZIP／上傳新版本」，沒有刪除。
  - Plugin Creator 回覆它不能刪除外掛。
  - 目前沒安裝，不會被呼叫，只是留在清單上。
  - 追蹤：issue #60；README 說明見 PR #61。
- **Open Design Cursor Tunnel v3：** 用的是「通道」連線，沒有網址欄位，不受主機名更改影響。目前沒建立連結，沒有實測。

## 2. 直接連 MCP 測試

| 項目 | 結果 |
|---|---|
| `initialize` 握手 | ✅ HTTP 200，`video-timeline-pipeline` v1.30.0（MCP SDK 版本） |
| `tools/list` | ✅ 9 個工具，約 0.9 秒：prepare_video_for_chatgpt、analyze_prepared_video、upload_prepared_video_to_drive、get_video_job、cancel_video_job、resume_video_job、get_prepared_video、collect_social_post、ingest_video_segments（2026-10-08 清單；#64 之後 main 共 10 個，另有 finalize_analysis_report） |
| `tools/call get_prepared_video` | ✅ 呼叫成功（用本機 cascade 跑的工作不在 MCP 工作清單裡，屬於正常情況） |

## 3. 路線 A：伺服器分析，在 ChatGPT 端從頭到尾實測

- **輸入：** `https://www.facebook.com/share/v/1ExtCtwPgg/`
- **流程：** prepare_video_for_chatgpt → analyze_prepared_video → ChatGPT 整理摘要
- **結果：** 影片 2:46，切成 2 段（0–120s、120–166.6s），總耗時 **4 分 26 秒**。
- **分析組成：** 語音用 Groq `whisper-large-v3`（87 段）；畫面用 MiniMax（17 筆，取樣 1 fps）。
- **結論：** Facebook 分享連結不再被安全檢查擋掉，修復有效。

## 4. 路線 B：ChatGPT 經 Google Drive 讀 MP4

- **步驟：**
  1. 在小工具按「上傳到我的 Drive」，約 2 分鐘。
  2. 開一般對話（不掛開發外掛），選 Google Drive 連接器，貼上交接內容。
  3. ChatGPT 下載原始 MP4、解碼、分析，約 **1 分 27 秒**。
- **SHA-256 驗證**（交接內容裡沒有雜湊值，所以不可能照抄）：

| 檔案 | 大小 | ChatGPT 算出的值 | 伺服器記錄 | 獨立下載重算 |
|---|---|---|---|---|
| segment_000.mp4 | 13,200,289 B | `53572b40…fadb26b4` | 一致 | 一致 |
| segment_001.mp4 | 4,358,503 B | `bfe01467…00642763` | 一致 | 一致 |

- **畫面驗證**（對照原片截圖）：
  - 0:30：上傳資料頁、三步處理流程 ✅
  - 1:05：CareArk 視窗、`127.0.0.1:58320` ✅
  - 2:10：「我／媽／+新增成員」選單、7 間醫院、¥1522.40、2026-08-14 ✅
  - 講者外觀描述也正確 ✅
- **限制：**
  - 環境裡沒有語音辨識，聽不到聲音。
  - 用程式抽幀看圖，屬於抽樣檢視，不是一次看完整支影片。
  - 有一處數字讀錯：4.98 讀成 4.96。

## 5. 品質比較（對照標準答案：原片每 4 秒一張截圖＋畫面字幕）

| 面向 | A：伺服器分析 | B：Drive 直讀 |
|---|---|---|
| 講了什麼、為什麼 | ✅ 大致完整（兩週、開源、DeepSeek、三種歸檔分類、陪父母看病的初衷；2:00–2:29 漏轉，見 02 §1） | ❌ 聽不到聲音，結尾沒有內容 |
| 畫面細節、數字 | ❌ 幾乎沒有 | ✅ 網址、金額、醫院數、選單都對 |
| 分段時間 | ⚠️ 講者回到全畫面寫成 2:29，實際約 2:18 | ⚠️ 較粗，0:20–0:45 混在一起 |
| 錯誤 | 說「2:00–2:29 旁白較少」（其實是漏轉） | 4.98 讀成 4.96；漏掉安裝步驟和 DeepSeek |
| 耗時 | 4 分 26 秒 | 上傳約 2 分＋分析 1 分 27 秒 |

**結論：** 兩條路互補。講話內容以 A 為主，畫面上的文字和數字以 B 為主，合併後品質最好。

## 6. 已確認的平台限制

- 伺服器規定掛開發外掛的對話裡不能用官方 Drive 連接器，所以 B 必須另開一般對話。
- MCP 工具回傳的內容裡沒有影片格式；回傳圖片的話，ChatGPT 端可能變成 `{}`，模型看不到（OpenAI 2026-04-22 的官方回覆）。
  - <https://developers.openai.com/apps-sdk/reference>
  - <https://community.openai.com/t/chatgpt-connector-returns-empty-for-mcp-tool-results-with-type-image-while-same-tool-works-in-mcp-inspector/1375446>
