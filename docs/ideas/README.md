# Video Timeline Pipeline：測試紀錄與改進想法

整理期間：2026-10-08 ～ 2026-10-10
測試影片：<https://www.facebook.com/share/v/1ExtCtwPgg/>（CareArk，2:46，job `16fae7864ca8`）
MCP 端點：`https://cursor-grokbot.tail930e77.ts.net/video-timeline/mcp`（2026-10-08 實測：initialize 回報 v1.30.0，這是 MCP Python SDK 版本而非本專案版本，當時 9 個工具；#64 合入後 main 為 10 個工具，新增 finalize_analysis_report）
狀態基準：本資料夾記錄 2026-10-08～10 的修正前實測；程式狀態已於 2026-10-11 對照 main 90734c1（含 #64／#67／#68／#69／#70）。

## 文件索引

| 檔案 | 內容 | 狀態 |
|---|---|---|
| [01-test-results.md](01-test-results.md) | 連線修復、路線 A／B 實測結果、品質比較 | ✅ 已驗證（2026-10-08 修正前基線） |
| [02-bugs.md](02-bugs.md) | 路線 A 的兩個 bug 和小錯誤，附證據與驗收標準 | 🔧 程式修正已合入 main（#64／#67／#69／#70），真實影片驗收未做，#55／#56 仍開 |
| [03-parallel-ab-spec.md](03-parallel-ab-spec.md) | A／B 並行＋合併的需求規格 | 🔧 §4.1–§4.3 已合入 main（#64），§4.5 真實影片驗收未做，#57／#58 仍開 |
| [04-uploadfile-library-experiment.md](04-uploadfile-library-experiment.md) | 用 `window.openai.uploadFile` 把影片存進 ChatGPT 檔案庫的實驗 | 🧪 待實測（#59 仍開；PR #66 的伺服器端做法方向錯誤，改走小工具端按鈕） |
| [05-merged-summary-careark.md](05-merged-summary-careark.md) | A＋B 合併、對照原片校正後的參考摘要 | 📄 參考輸出 |

## 重點結論

1. **連線已修好：** Funnel 主機名改成 `cursor-grokbot` 後，ChatGPT 改用新建的外掛「Video Timeline Pipeline - cursor-grokbot」，實測可用。
2. **Facebook 分享連結的修復有效：** 透過 ChatGPT 從頭到尾跑通（下載 → 切段 → 分析 → 摘要，共 4 分 26 秒）。
3. **路線 A（伺服器分析）：** 講話內容大致完整，但有兩個 bug（修正前實測）：Whisper 漏轉約 30 秒、MiniMax 畫面時間碼錯誤；修正已合入 main，狀態見 02。
4. **路線 B（ChatGPT 經 Drive 讀 MP4）：** 確認 GPT 真的拿到影片：SHA-256 三方一致，指定時間點的畫面描述也全部正確。缺點是聽不到聲音。
5. **兩條路互補：** A 負責講了什麼，B 負責畫面上的數字和文字。合併後品質最好（見 05）。
6. **外掛沒辦法直接把影片交給 ChatGPT 的模型：** 依官方文件和社群回報，B 目前只能走 Drive，或走待驗證的檔案庫路線（見 04）。

## 對應的 Issues（已建立，截至 2026-10-11 皆為 open）

| 標題 | 類型 | 參考 | Issue／PR |
|---|---|---|---|
| Whisper 在第二段開頭漏轉約 30 秒（120.0–149.6s） | bug | 02 §1 | #55（#64／#67／#69 已合入，未實測） |
| MiniMax 畫面證據時間碼壓縮在 0–6.9s | bug | 02 §2 | #56（#64／#70 已合入，未實測） |
| A／B 並行：prepare 後自動同時跑分析和 Drive 上傳 | feature | 03 | #57（#64 已合入） |
| A 完成後把結果存成 `A_server_analysis.md` 放進 Drive，並提供合併指令 | feature | 03 | #58（#64 已合入；需 finalize_analysis_report） |
| 實驗：`uploadFile(file, {library:true})` 存 MP4 到 ChatGPT 檔案庫 | experiment | 04 | #59 |
| ChatGPT 外掛清單殘留「Cursor Fixed」，介面上刪不掉 | chore | 01 §1 | #60（README 說明見 PR #61） |
