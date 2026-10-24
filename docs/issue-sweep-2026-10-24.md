# 2026-10-24 issue 修復與驗收盤點

基準：`main@7c3ba8073afcc218fe34f8b6dd49276788071315`。本次帳號完整清單中，本專案有 18 筆 open issue。以下區分本次實作、既有候選 PR 與尚需取得的驗收證據；沒有把未合併候選、離線 mock 或過去的 CI 當成正式服務已修復。

## 本次可獨立驗證的修正

| Issue | 修正與回歸證據 |
|---|---|
| #126 | `files.create` 後以完整 `files.get` 重讀資料夾；以真實 fields 遮罩的 API fake 驗證首次知識檔上傳。 |
| #127 | 在 fetch 前排除未允許的付費供應商，保留既有錯誤；允許供應商失敗仍使 CLI 回 1。 |
| #128 | 設定最短間隔時，初次付費前持久保存訂閱與時間戳；中斷後保留 pending 訂閱，排程不重複付費。未設定間隔時保持原欄位。 |
| #129 | 作者與單篇採集把大小寫正規化後的 canceled/cancelled 當終態，不睡到逾時、不重建工作，不釋放既有費用預留。 |
| #130 | 尾段驗證採影像串流時長；移除經 probe 證明沒有影像的尾段；完整 copy 分段可重用，未完成 copy 仍重切。含影像但 CSV 壞掉的檔案不會被誤刪。以 4 秒影像／5.2 秒音訊實檔驗證 copy/transcode 與第二次零編碼。 |
| #131 | chunk end 與偏移後絕對時間碼限制在原片長度內，超界保留不可靠標記；合併快取版本升為 3。以 libx264 實檔與模擬片尾模型回應驗證讀取後仍可跳轉。 |

測試入口：`tests/test_issue_sweep_regressions.py`、`tests/test_rss_paid_discovery.py`、`tests/test_split_mode.py`、`tests/test_split_resume.py`；完整檢查為 `python scripts/check_offline.py`。測試使用臨時資料夾、假憑證與 API mocks；沒有送出付費工作、操作正式 Drive 或部署。

## 其他 12 筆的具體下一步

| Issue | 已有工作／本次查證 | 尚未完成的條件 |
|---|---|---|
| #105 | PR #107（`c11188e`）保留補漏端點並加上快取 receipt，仍與 main 衝突。 | PR 自審明列 `fixed=false`：任意長尾段只要有片尾事件就可被視為覆蓋。需先界定事件時間點與時段覆蓋的語意，保留端點、拒絕仍有空檔的快取後再重跑。不能直接合併其未解缺口。 |
| #60 | 現有文件記錄為 Workspace upload 外掛清單殘留。Plugin Creator 現有工具沒有刪除操作；目標搜尋只返回其他公開外掛，未取得該 workspace 物件。 | 需要該外掛確切 workspace 身分及平台支援的 Library 刪除操作。不是卸載已安裝 App，也不是 repo 程式修補。 |
| #59 | 已將 PR #89（`7132ce0`）的片段範圍快照與驗證點換算移植到本次候選，另將現有 v17 升為 v18；42 項 library 控制通過。 | 在支援 uploadFile 的真實 ChatGPT host 驗證 file_id、SHA-256 與指定時間點畫面；缺能力時必須保留不支援回饋。 |
| #58 | 原 PR #64 已整合 A 報告交接；main 有報告與離線測試。 | 真實 Drive 同資料夾的 A_server_analysis.md、實際完成提示與複製／合併流程。 |
| #57 | 已移植 PR #90（`2410060`）的 A／Drive 獨立 polling 及刷新恢復修正；50 項 widget 控制通過。 | 以真實 prepare 驗證 A／Drive 啟動差小於 10 秒、A 未完成時仍能交付 B。 |
| #56 | PR #91（`474e41a`）補補漏回應時間碼驗證及 cache revision，與 main 衝突。 | 原始 166.6 秒媒體與真實模型回應，逐筆對照截圖誤差不超過 5 秒。 |
| #55 | 已移植 PR #92（`6ba1152`）拒絕缺少 start/end 補轉結果的修正，另拒絕 JSON 布林值冒充時間；8 項 schema 回歸通過。 | 原始 166.6 秒媒體重新轉錄，確認 2:00–2:28 字幕實際出現且無超過 5 秒的內部缺口。 |
| #23 | PR #95（`d10bdc9`）補 file reference 接收與正式 schema，與 main 衝突。 | 真實 host 呼叫 ingestion，收到可核對 file_id/download_url，代表影格可視，不能用 metadata 摘要代替。 |
| #10 | PR #16（`0faaa19`）只有研究設計；issue 的固定抽幀 baseline 已與 main 的整片 MP4 理解不同。 | 先更新 baseline，再以凍結的 12–20 支素材做 bounded escalation 對照，輸出 BUILD/NARROW/REJECT；schema 或供應商宣稱不能代替研究。 |
| #7 | PR #94（`113a72f`）有 F0 storage foundation，PR #17 有 roadmap 文件；均與 main 衝突。 | 先整合唯一 migration／library identity，再驗證既有資料遷移、outbox、重啟與重建相容性；roadmap 本身不等於各階段功能已完成。 |
| #6 | PR #97（`388b998`）有 Dashboard／durable tasks／API 與 UI 測試，與 main 衝突。 | 在共用 storage/search service 整合後重跑安全、恢復、成本與本機 browser 驗收；不自動公開服務或啟用寄送。 |
| #5 | PR #96（`10f4d69`）有 retrieval、引用身分、cache lifecycle 與 25 題合成評估，與 main 衝突。 | 先解與 main 及 F0 的整合衝突，保留舊向量，再完成有人工 golden evidence 的 baseline/hybrid 對照。 |

## 後續整合順序

先交付 #126–#131 的小範圍修正。既有大 PR 依 F0（#94）→ retrieval（#96）→ Dashboard（#97）的依賴順序逐一重建於最新 main；媒體／host 修補則以各自的回歸測試審查後整合。每層先跑針對性測試，整合完成才跑一次完整 gate；付費、真實媒體、Drive、ChatGPT host 與研究結論使用各自的驗收證據。

全部 18 筆在本次盤點時仍為 open；此文件不變更 GitHub issue 狀態。
