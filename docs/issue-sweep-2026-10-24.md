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
| #105 | 在獨立媒體分支完成安全來源修補：保留提示端點，按原 issue 的 30 秒上限重查全部間隔及 chunk 接點；不採用 PR #107 的任意長片尾豁免。新 receipt 保留剩餘空檔，CLI／MCP／F0 不把 partial 當成完成或可重用快取。 | 來源層的端點與快取正確性由離線回歸驗證；模型能否補出真實畫面屬於媒體／provider 品質驗收，不能由 fake 回應推定。 |
| #60 | 現有文件記錄為 Workspace upload 外掛清單殘留。Plugin Creator 現有工具沒有刪除操作；目標搜尋只返回其他公開外掛，未取得該 workspace 物件。 | 需要該外掛確切 workspace 身分及平台支援的 Library 刪除操作。不是卸載已安裝 App，也不是 repo 程式修補。 |
| #59 | 已移植 PR #89 的片段範圍快照與驗證點換算；媒體分支升為 v19，另有明確 ingestion 回執檢查，50 項 library 控制通過。 | 在支援 uploadFile 的真實 ChatGPT host 驗證 file_id、SHA-256 與指定時間點畫面；缺能力時必須保留不支援回饋。 |
| #58 | 原 PR #64 已整合 A 報告交接；main 有報告與離線測試。 | 真實 Drive 同資料夾的 A_server_analysis.md、實際完成提示與複製／合併流程。 |
| #57 | 已移植 PR #90（`2410060`）的 A／Drive 獨立 polling 及刷新恢復修正；50 項 widget 控制通過。 | 以真實 prepare 驗證 A／Drive 啟動差小於 10 秒、A 未完成時仍能交付 B。 |
| #56 | 已移植 PR #91 的補漏回應時間碼驗證與 cache revision，保留目前上傳重用及來源時間邊界修正。補漏時間壓縮與指定時段回歸已通過。 | 原始 166.6 秒媒體與真實模型回應，逐筆對照截圖誤差不超過 5 秒。 |
| #55 | 已移植 PR #92（`6ba1152`）拒絕缺少 start/end 補轉結果的修正，另拒絕 JSON 布林值冒充時間；8 項 schema 回歸通過。 | 原始 166.6 秒媒體重新轉錄，確認 2:00–2:28 字幕實際出現且無超過 5 秒的內部缺口。 |
| #23 | 已移植 PR #95 的嚴格 file reference／有限時間範圍 schema、prepare busy DTO 與明確 widget ingestion；Python 契約、HTTP MCP 及 50 項 library 控制通過。 | 真實 host 呼叫 ingestion，收到可核對 file_id/download_url，代表影格可視，不能用 metadata 摘要代替。 |
| #10 | 已整合 PR #16 的歷史設計，並在 `docs/research/issue-10-evidence-escalation.md` 改為現行整片 MP4 baseline，補上 16 片分層、人工 gold、paired evaluation 與執行邊界。舊抽幀 spike 不作為現行決策。 | 實際 12–20 支原片及授權、人工標註／覆核、凍結品質／延遲／成本門檻與授權的真 provider 執行；尚無可支持 BUILD/NARROW/REJECT 的研究結果。 |
| #7 | 獨立整合分支依序合併 PR #94 → #96 → #97，F0 migration 1–4 與明確啟用的 Web migration 5 共用唯一 registry。索引重建保留帳本 library ID，舊索引 ID 可由首次帳本建立時沿用；衝突 ID 明確拒絕。 | 離線資料遷移／outbox／重啟／重建控制已驗證；仍需正式資料升級驗收，其餘 roadmap 階段不能由 F0 完成推定。 |
| #6 | 已在整合分支解決共用 schema、唯讀報表、catalog、filter、RRF、引用及 paid Ask 設定檢查；Web 38 項與跨功能 10 項回歸通過。 | 本輪僅 TestClient 與 Node，沒有可用 browser automation 工具；需新鮮本機 browser／手機尺寸與正式資料驗收。不自動公開服務或啟用寄送。 |
| #5 | 已在整合分支保留 canonical retrieval、舊向量 namespace／adoption、occurrence ID 及過濾語意；Web Ask 在核對來源前保留原始 evidence，輸出才遮蔽網址。 | 合成回播與來源變更控制已驗證；仍需人工 golden evidence 與真模型 baseline/hybrid 品質對照。 |

## 後續整合順序

先凍結九項小範圍修正（#126–#131、#55／#57／#59），保留該分支不可變。既有大 PR 已在另一 worktree 依 F0（#94）→ retrieval（#96）→ Dashboard（#97）的依賴順序整合；媒體／host 修補則以各自的回歸測試審查後整合。每層先跑針對性測試，整合完成才跑完整 gate；付費、真實媒體、Drive、ChatGPT host 與研究結論使用各自的驗收證據。

全部 18 筆在本次盤點時仍為 open；此文件不變更 GitHub issue 狀態。
