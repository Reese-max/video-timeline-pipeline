# Roadmap — 自動影片情報系統（Issue #7 r2 快照）

[Issue #7 r2](https://github.com/Reese-max/video-timeline-pipeline/issues/7)
的規劃摘要快照（2026-09-05 第二輪複核；程式基準
[`cd93da7`](https://github.com/Reese-max/video-timeline-pipeline/tree/cd93da7d28c8bff039fb4fb1fd0381d619325020)）。
**本文件保存當時規劃，不代表功能已實作、測試或部署；後續討論與狀態以 Issue 為準。**
產品目標：把既有下載/轉錄/畫面分析/知識庫
補成「主動寄摘要、可追問、可監控」的本機優先系統。**先讓每日摘要可用**。

## 既有能力與非重做範圍

以下保存 r2 基準的能力盤點，後續實作前仍須依目前程式重新確認；沿用既有能力，不重做：

- 下載／轉錄／畫面分析／摘要與分層快取已有管線；新增服務沿用其產物與恢復路徑。
- 搜尋已有 SQLite FTS5、短中文字詞回退、分類篩選、時間戳與來源引用。
- Ask（`--ask`）由 MiniMax 回答；OpenRouter 用於 embedding，另有本機 BGE-M3 後端，
  以 `--semantic` 選用語意檢索；rerank 尚未接入，不能把端點實測當成整合完成。
- RSS 佇列／重試／dead-letter／single writer 已存在，新增通知與監控只做整合。
- 既有 `--digest` 會呼叫 MiniMax；#2 本機 Digest 預覽只組裝既有 summary，外部 API 呼叫=0。

## 已修正的假設（r2 對照）

| 原假設 | 修正 |
|---|---|
| 本地唯一鍵＝遠端不重寄保證 | #2：provider 去重有效期＋accepted/unknown＋人工處置 |
| Daily/Weekly 窗口互相排除 | #2：明確截止窗口＋遲到/carry-over |
| 預覽誤用會呼叫 MiniMax 的 digest | #2：preview 外部呼叫=0，只組裝既有 summary |
| score 唯一鍵遺漏內容版本 | #3：加 `current_input_hash`/`analysis_revision` |
| Token Plan 用量被當免費 | #4：`unknown`/`confirmed_included` 分開；sent reservation 不因逾時釋放 |
| 規劃重造 RSS retry/queue | #2/#4/#6：沿用既有，新增的是整合不是第二套狀態 |
| 新表塞進會 DROP/REPLACE 的搜尋 DB | F0：`operations.db`（永久）與 `library.db`（可重建）分離 |

## F0 — 共用基礎工作包

F0 由首個基礎 PR 負責，在 #2/#3/#4 開始寫 DB 前完成，不各自建立第二套 migration、event 或 job 身分。

- **F0.1 儲存責任**：`operations.db` 保存永久操作帳與唯一 migration registry；
  `library.db` 是可重建的搜尋投影，保留有效向量。先盤點並處理 legacy `DROP`／`REPLACE`
  與 positional SQL（如 `INSERT INTO ... VALUES (...)`）的相容性；擴充 schema 時改用顯式欄位清單，
  不得把永久操作資料接到搜尋表，並以舊資料升級／重建回歸確認操作帳與有效向量不遺失。
  讀取不隱式 DDL，未初始化回報
  `unavailable`/`migration_required`；DB、檔案與 RSS JSON 不宣稱共用交易。
- **F0.2 穩定身分**：持久保存 `library_id` UUID，`analysis_revision` 綁實際內容與版本，
  不綁路徑或執行時間。`processed_revisions` 完成登記與 deterministic outbox event
  在同一 `operations.db` 交易提交，漏登記由 reconciliation 補掃；歷史 backfill 時間未知就標未知，預設不寄。
- **F0.3 交易與恢復**：每個 worker 獨立 connection、短交易與 rollback，網路在交易外；
  lease 帶 owner/fencing token，送出前持久化 attempt。已送但結果不明保留 `unknown`，
  lease 過期不授權重送；以 SQLite backup API 或停寫備份，還原後先停外送並對帳。
- **F0.4 狀態 owner**：NotificationService 唯一負責 delivery/retry，RSS service 保持原 single writer；
  Usage/Budget service 管預留與結算。Search adapter 先包既有 FTS/semantic，Scoring 不等待新 hybrid；
  通用 `operation_tasks` 到 #6 W7 才擴充。
- **F0.5 設定與安全**：保留舊 CLI 與 `.env` 優先順序；新旗標、exit code 先列契約並測相容。
  Email/scoring/rerank/排程/遠端監聽預設不啟用，不繞過 local-only、dry-run、batch opt-in；
  遮罩秘密、忽略 DB/WAL/SHM 與憑證，影片或模型中的指令只作資料。

## 修正後的依賴與交付順序

```text
基準盤點／測試樣本＋F0 持久化契約
  → #4 C0–C3 最小用量／預算介面與 fake
      ├─ #2 N0–N5 本機 Digest 預覽
      │    → #2 N2 單一 provider＋N6 故障處置＋N8 排程 → Email MVP
      └─ #4 C4–C8 各 provider 接入（可平行）
  → #3 S0–S7 評分／校準／shadow mode
  → #2 N7 高重要性與聚合告警
  → #5 A0–A10 搜尋與可核對問答
  → #6 W0＋W8 安全基礎 → 只讀 UI
  → #6 W7 持久化任務 → Ask／重試／Digest 送出
```

只讀預覽可與成本工作平行。真實寄送需要該 provider 的授權、用量／未知成本政策與安全 retry，
不必等待其他所有 provider 或 #3/#5；Scoring 先用既有 FTS adapter，避免與 #5 循環相依。
W8 必須在 Web 副作用功能之前完成，不能因 UI 排在後面而延後安全基礎。

## 版本節點與 Release Gates

版本號是規劃，並非已建立 release/milestone 或完成驗收。下列條件均須另有測試與驗收證據。

| 版本 | 工作包 | 必要驗收條件（待驗收） |
|---|---|---|
| v0.3a | #2 N0–N6／N8、F0、#4 最小基礎 | preview 外部 API 呼叫=0；使用者確定收件者與 provider 授權；日／週窗口、catch-up、carry-over、immutable snapshot、unknown／安全 retry 已測；排程明確安裝才啟用。accepted 不等於 delivered，無遠端去重契約不承諾 exactly-once。 |
| v0.3 | #4 C0–C8、#3 S0–S7、#2 N7 | 所有啟用的付費路徑經 guard/usage；unknown 或 subscription quota 不當 0。hard 模式無可證明上限則拒絕，sent reservation 不因 timeout 釋放；novelty 缺歷史為 null，有 cutoff／引用與內容 hash；校準／shadow mode 可查誤報，重評分不重寄；告警聚合不遞迴。 |
| v0.4 | #5 A0–A10 | 舊 CLI、短中文字詞與有效向量不回退；hybrid/filter 與 baseline 比較；引用 occurrence 與向量身分分開，無真時間不偽造 0 秒；主張可核對來源，prompt injection 不觸發操作；新舊索引可切換並評估成本／token／峰值 RAM。 |
| v0.5 | #6 W0–W11（W8 不延後） | 只讀頁面不 DDL、不下載模型、不付費，先有 Host/Origin/XSS 防護；Ask/retry/send 經授權、CSRF、預算與 W7 可恢復 task；非 loopback 同時需要 opt-in、authentication、可信加密通道；誠實呈現 crash/unknown/cancel/accepted；有 API/browser smoke、資源與舊流程回歸證據。 |

## Phase 2 / v0.6 backlog（不阻擋 MVP）

1. 跨平台去重（URL canonicalization → 音訊/影格指紋）
2. Entities/Actions 型別化（不從零重寫摘要）
3. Delta 摘要（對凍結歷史證據比較新增/修改/衝突）
4. Quality-aware routing（任何降級須獨立 opt-in + 品質評估）
5. 更多通知/知識庫輸出（沿用既有授權/去重/unknown 處置）

## 交付節奏

每工作包小 PR：契約/migration + fixtures → pure service + fake →
單一 adapter → CLI/API → 整合/文件。**不一次大改 pipeline.py 同時完成
五張 Issue。**

開始前先確認既有實作、輸入／輸出、狀態 owner、migration、副作用、費用 scope、快取鍵與恢復路徑。
測試策略包含 pure unit、fake integration（429/auth/unknown、雙 worker、provider 接受後 crash、
預算跨日、DB 忙／磁碟滿、partial 向量回應）、舊資料升級與本機回歸；預設封鎖外部網路與真實憑證。
真 provider canary 獨立 opt-in 並限制樣本／費用，寄信另需明確收件者與授權。
量測附 commit、機器、資料量、模型與 cache 狀態；未測不標完成，不把整個資料庫當測試。

## 待使用者決定

收件者、寄件服務/網域、日/週截止時間、真 API 測試費用、部署機器、
是否需要遠端介面。目前只提供方案，不自動啟用任何服務。

## 回復與資料保全

- 關 flag 不撤回已寄郵件/已送出付費工作。
- 還原 DB 前先停 worker 並備份；舊備份可能缺最新 accepted/reservation，
  需對帳後才恢復外送。
- 刪搜尋投影不能刪寄送/成本/決策稽核；purge 另設保留期限。
- 新 chunker/模型採 shadow generation，不清空可用索引再冒險重建。
