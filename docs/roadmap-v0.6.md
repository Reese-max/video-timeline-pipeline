# Roadmap — 自動影片情報系統（Issue #7 r2 快照）

Snapshot of the consolidated roadmap in `Reese-max/video-timeline-pipeline#7`
（2026-09-05 第二輪複核）。產品目標：把既有下載/轉錄/畫面分析/知識庫
補成「主動寄摘要、可追問、可監控」的本機優先系統。**先讓每日摘要可用**。

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

## 待使用者決定

收件者、寄件服務/網域、日/週截止時間、真 API 測試費用、部署機器、
是否需要遠端介面。目前只提供方案，不自動啟用任何服務。

## 回復與資料保全

- 關 flag 不撤回已寄郵件/已送出付費工作。
- 還原 DB 前先停 worker 並備份；舊備份可能缺最新 accepted/reservation，
  需對帳後才恢復外送。
- 刪搜尋投影不能刪寄送/成本/決策稽核；purge 另設保留期限。
- 新 chunker/模型採 shadow generation，不清空可用索引再冒險重建。
