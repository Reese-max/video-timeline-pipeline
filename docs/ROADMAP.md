# 影片知識庫路線圖（#7）

以 #7 第二輪複核的工作包與 release gates 為準。版本名稱是規劃，不代表 release、部署或功能驗收完成；這份文件不替其他 issue 勾選完成。

## 本次交付與驗收對照

本次實作 F0.1／F0.2 的共用持久層，沿用現有通知、成本、評分與 RSS service；不一次實作 #2–#6。

| 要求 | 機制／離線驗證 |
|---|---|
| 唯一 migration registry | `operations.py` 的 4 個版本集中管理既有通知、成本、評分 DDL；每個 migration 先 `BEGIN IMMEDIATE`，失敗時 schema 與 registry 一起 rollback；並行初始化測試 |
| 既有資料相容 | 採 `CREATE IF NOT EXISTS` 與具名欄位；既有成本資料、缺少 reservation link 的 schema 原地升級，不替換 audit rows |
| 真正只讀介面 | 所有 operations 報告、評分解釋使用 `mode=ro`；未初始化為 unavailable，舊版／未來版為 migration_required；測試 DB 不存在與 bytes 不變 |
| 穩定資料集身分 | `library_meta` 持久 UUID，並行建立一致，搬移 DB 保留、新空資料集不同 |
| 穩定內容版本 | 驗證 transcript／timeline／summary 與已宣告視覺產物，拒絕無效事件、時間戳與溢位數值；內容、來源 SHA-256 與模型版本參與身分，執行時間、分類、快取統計、絕對媒體路徑排除 |
| 完成登記＋outbox 同交易 | `processed_revisions` 與既有 `notification_events` 同一 SQLite 交易；deterministic event key 含 library／job／revision；測試強制 outbox 失敗後兩邊都無資料 |
| 重跑不成為新影片 | `(library_id, job, analysis_revision)` 主鍵保留首次時間；並行重跑只產生一個版本與事件；歷史補登暫時失敗後，相同內容仍不具通知資格，真正變更仍可登記 |
| 保守 reconciliation | `--ops-reconcile` 補登遺漏版本及事件，backfill 的 first_completed_at／eligible_at 均 NULL，不呼叫寄送 adapter；可重跑 |
| Digest 不誤寄歷史 | 核對現有產物的精確 revision 及實際渲染摘要的原始 bytes 雜湊，拒絕未登記修改與不可讀帳本；支援管線的 short_summary，無 created_at／mtime fallback |
| 搜尋與永久帳本分離 | 搜尋重建不碰 operations，保留 `(text_hash, model)` 向量快取；videos INSERT 改具名欄位以容納相容擴充 |
| 舊 CLI 與 owner | 原旗標語意、RSS JSON／single writer、Notification service、成本 guard 與評分入口保留；即時寄送僅領取自己的 delivery，Digest item 狀態以參數寫入；新補登命令不委派付費或寄送 |

驗證入口：`python -X utf8 -B scripts/check_offline.py`（專案依賴已安裝時），涵蓋完整 Python 與四組 Node widget 測試；`tests/test_operations.py` 提供 F0 合成資料回歸，不讀正式影音或憑證。

目前沒有設定獨立 lint、coverage ratchet、CRAP 或 mutation gate；本次不新增工具或門檻。Windows 的 Git Bash 下，既有清空環境的子程序測試會觸發 `WinError 87`；完整檢查以原生 PowerShell 執行，driver 的 F0 驗證命令另以 Git Bash 實跑。

本機驗證：`uv run --no-project --with-requirements requirements.txt python -X utf8 -B scripts/check_offline.py` 結束碼 0；PR 自我審查新增 7 項回歸，Python 由審查前 639 項增至 646 項（622 通過、24 略過），四組 Node 共 94 項通過，`git diff --check` 通過。evidence 的 Git Bash 命令另有 25 項 F0 回歸全數通過，並保留修正前的失敗證據；通知測試固定日期，不依賴當天截止時間。略過的既有平台／外部驗收不算通過。

## 儲存責任

| 儲存 | 權威資料／生命週期 |
|---|---|
| `jobs/` | 原分析產物與來源證據；有效產物可重用 |
| `library.db` | 可重建搜尋投影、FTS 與共用向量快取 |
| `rss/state.json` | 原 RSS 排程／佇列；仍由原 single writer 管理 |
| `operations.db` | 資料集身分、完成版本、通知／摘要、成本／預算與評分稽核 |

四者沒有共同交易。完成事件 commit 後 manifest 投影失敗仍可能發生，不能以 manifest 是否更新判斷 DB 是否提交。原始 bytes 雜湊記錄首次登記產物；內容版本排除執行資訊，因此暖快取的 provenance bytes 可不同但版本相同。

## 依賴與交付順序

1. 基準盤點、合成測試樣本、F0 共用持久化契約。
2. #4 C0–C3 最小用量／預算介面與 fake 測試；#2 N0–N5 零外部 API 的本機預覽可平行。
3. #2 單一寄件 provider、N6 故障處置、N8 明確 opt-in 排程；真實送出需收件者、provider 授權與未知成本政策。
4. #4 C4–C8 已啟用 provider 路徑、#3 S0–S7 評分與 shadow 校準，再接 #2 N7 聚合告警。
5. #5 A0–A10 真正 hybrid、filter、引用驗證及退化策略；保留短中文字詞與有效向量。
6. #6 W0／W8 安全基礎先於只讀 UI；W7 持久化 task 先於付費 Ask／retry／send。

Scoring 先使用現有 FTS adapter，不等待新的 hybrid；Email MVP 不必等待全部 provider、評分或 Dashboard。

## 尚未完成的 release gates

- F0.3：一致性備份（SQLite backup API／停寫清單）、回復後停寄與停新付費工作的對帳流程、部署副本身分／root 鎖；現有檔案鎖不代表已防止備份副本在兩台機器寄送。
- F0.3／F0.4：通知 attempt 在傳輸前持久化、過期 sending 的 unknown 恢復與完整 fencing 驗收；成本 unknown／reservation 保留不代表已有人工對帳工具。本次不自動釋出 reservation、不重試 unknown。
- F0.5：跨入口一致的新版 exit code 契約尚未遷移；既有 exit code 保持相容。新讀取狀態在 JSON 明示 unavailable／migration_required。
- v0.3a：Daily／Weekly coverage、immutable snapshot、carry-over、catch-up、provider 去重有效期與故障演練；accepted 不能宣稱 delivered，也不承諾任意故障下 exactly-once。
- v0.3：全部付費路徑的硬上限證據、novelty cutoff／歷史引用、評分校準與非遞迴告警仍需各工作包的驗收。
- v0.4：兩路融合的 hybrid、filter baseline、主要主張引用核對、generation 切換及成本／RAM 評估。
- v0.5：Dashboard Host／Origin／XSS、CSRF、authentication／可信加密通道、durable task 的 crash／unknown／cancel，以及 API／真實瀏覽器驗收。

Phase 2／v0.6（不阻擋 MVP）：跨平台內容去重、Entities／Actions 擷取、凍結歷史上的 Delta 摘要、具品質評估的 routing、更多通知與知識庫輸出。影片／模型中的工具指令一律作資料，不執行命令、不改收件者。

## 操作與驗收邊界

未設定收件者、正式寄件 provider、正式日／週截止時間、真 API 測試預算或部署機器。本次沒有呼叫真 provider、寄信、設定排程、部署或驗證公開服務。升級前應以現有維運程序保全 DB／RSS／manifest；不在寫入中只複製主 DB 而忽略 WAL。還原舊備份前先停 worker、保全現況，再人工核對 accepted／unknown／reservation，不能以功能 flag 當成撤回遠端工作的手段。
