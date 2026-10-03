# Roadmap — 影片時間軸管線 → 自動影片情報系統

來源 Issue：[#7](https://github.com/Reese-max/video-timeline-pipeline/issues/7)（第二輪複核 r2，2026-09-05／產品服 2026-09-06 投資決策：先讓每日摘要可用，不等完整評分、RAG 與 Dashboard）。

本文件是 repo 內的持久路線圖快照；Issue 保留規劃決策全文，程式行為以程式與測試為準。checkbox 只在有證據的驗收後勾選。

## 既有能力（非重做範圍）

FFmpeg／Groq／MiniMax、多層快取、RSS 重試／dead-letter／Webhook／Windows 排程、FTS5 與短字詞 fallback、類別 FTS 搜尋、text-hash 向量快取、semantic Search／MiniMax Ask、全庫 digest、Open Notebook 同步與 `--audit`。

## 規劃實作

郵件日／週 Digest、可信的用量與預算層、影片評分、真正兩路融合的 hybrid、程式化引用驗證、Web 介面。

## F0 — 共用基礎工作包（其他 Issue 寫 DB 前完成）

- [ ] 唯一的 operations.db migration registry 與版本；library 調整採具名欄位或獨立衍生表並測相容；讀取介面不隱式 DDL／migration。
- [ ] 每個資料集持久保存 library_id UUID；job 沿用來源識別；analysis_revision 根據實際分析內容產生；新增 `processed_revisions(...)`；verified completed 產物登記與 outbox 事件在同一 operations.db 交易完成。
- [ ] 每個 worker 獨立 connection、明確 transaction 模式／busy timeout；短交易只處理狀態；lease 有 owner＋fencing token；未送出的本機工作可恢復，已送結果不明則保留 unknown。
- [ ] NotificationService 負責唯一寄送與 retry；RSS service 仍掌管排隊；Usage／Budget service 負責 attempt 預留／結算。
- [ ] 舊 CLI 旗標語意保留；新命令預設停用；no-video／local-only／dry-run／batch opt-in 不能被新入口繞過；`.env` 讀取優先順序保持相容。

## 依賴與交付順序

```text
基準盤點／測試樣本＋F0 持久化契約
             ↓
#4 C0–C3 最小用量／預算介面與 fake 測試
       ┌─────┴─────────────────┐
       ↓                       ↓
#2 N0–N5 本機 Digest 預覽      #4 C4–C8 各 provider 接入
       ↓                       │
#2 N2 單一 provider＋N6 故障處置＋N8 排程
       ↓                       │
     可用的郵件 MVP             │
       └──────────┬────────────┘
                  ↓
#3 S0–S7 評分／校準／shadow mode
                  ↓
#2 N7 高重要性與聚合告警
                  ↓
#5 A0–A10 強化搜尋與可核對問答
                  ↓
#6 W0＋W8 安全基礎 → 只讀 UI
                  ↓
#6 W7 持久化任務 → Ask／重試／Digest 送出
```

## 版本節點（規劃，不代表已建立 release）

- **v0.3a Email MVP**：preview 零外部 API、日／週窗口與 carry-over、immutable snapshot、排程明確安裝時才啟用、accepted 不宣稱 delivered。
- **v0.3 Intelligence／Cost／Alerts**：用量 guard 與 usage、unknown／subscription quota 不當 0、novelty 缺歷史為 null、calibration 與 shadow mode、告警聚合且不遞迴。
- **v0.4 Reliable Search／Ask**：舊 CLI 與有效向量不回退、hybrid 與 filter 有 baseline、引用身分與共用向量分開、主要主張可核對來源、新舊索引可切換。
- **v0.5 Operations UI**：read-only 頁面不做 DDL／不付費、Host／Origin／XSS 防護先有、付費動作經授權與 CSRF 防護、crash／unknown／cancel 誠實呈現。

## 測試策略

Unit（hash／decision／窗口／pricing／RRF／filter／escaping）；fake integration（429／auth／unknown、雙 worker、provider 接受後 crash、預算跨日、DB 忙）；舊資料升級；本機回歸預設封鎖外部網路與真實憑證；真 provider canary 獨立 opt-in。

## Phase 2／v0.6 Backlog（不阻擋 MVP）

跨平台去重、Entities／Actions、Delta 摘要、Quality-aware routing、更多通知／知識庫輸出。

## 來源

- [程式基準 pipeline.py](https://github.com/Reese-max/video-timeline-pipeline/blob/cd93da7d28c8bff039fb4fb1fd0381d619325020/pipeline.py)、[README 基準](https://github.com/Reese-max/video-timeline-pipeline/blob/cd93da7d28c8bff039fb4fb1fd0381d619325020/README.md)。
