# Issue #5 — 檢索與可核對問答驗收

日期：2026-10-17。範圍：`issue-loop/issue-5`，基準 `42d225936ba704cf70b93cac7bdf84da85ac86b8`。
只修改本機程式、測試與文件；沒有推送、建立 PR、部署、真實 provider 請求或重算使用者資料。

## 驗收對照

| 條件 | 結果與機制 |
|---|---|
| A0 基準／相容 | 保留 trigram、短詞 LIKE escaping、phrase quoting、既有 limit／model 名稱；固定回播直接載入 admitted base 的原始程式。 |
| A1 引用身分 | 獨立 evidence projection，持久 library ID＋job＋artifact revision＋kind＋來源 pointer＋全文 hash；複製 DB／無內容變更的重建保持 ID。同文不同時間保留，title／summary 為 null 時間。 |
| A2 來源／分段 | 沿用既有 title／speech／visual／summary／chapter，保存 chunker_version、segment IDs、start/end 與原始 metadata。未強制重新分段；視覺／摘要只能標模型衍生證據。 |
| A3 Hybrid／資源 | eligible-set 在 FTS／vector Top-K 前一致套用；依 rank 做 RRF（k=60），以 ID 決定同分順序；最多 1000 候選。向量用游標掃描、O(limit) 保留集合。缺能力可見降級，strict 明確失敗，預算／取消／deadline 不吞掉。 |
| A4 Metadata | SearchFilter 共用 platform／author／exact category／date／job／kind；未知日期預設排除並計數。明確區分發布、首次可核對完成與索引時間；本機來源 backfill 保留 provenance，不猜作者與日期。查詢不做 DDL／migration。 |
| A5 Grounding | 對本次提供的 ID、完整 quote、實際來源 pointer／文字／時間／revision 做核對。只放行完整摘錄及有限 attribution，其他改寫拒絕；模型不產生 URL／時間連結。人類輸出標示支持類型，視覺不可冒充講者或 OCR，來源指令不觸發外部動作。 |
| A6 CLI | 新 flags 集中 `register_retrieval_cli`；舊 flags 保留。JSON 單一文件，plan 與其他 action 互斥。safe 模式不要求 MiniMax model/key；Ask 不呼叫 provider，embedding 只列計畫，semantic 可見降級。audit 沿用原入口。 |
| A7 Embedding | backend／model／revision／dimension／preprocessing namespace；完整 hash＋原文碰撞核對。驗證 count、index、finite、非零與 float32；invalid 批次記錄，shadow 完整驗證後啟用。舊表保留，明確 adoption 才映射，不預設付費重算。ML lazy load、單一常駐模型、256 批次、查詢不下載模型。 |
| A8 Context | 跨影片輪流取樣，單片不設三段限制；以 UTF-8 bytes 上界計入指令、問題、metadata、framing、輸出預留。完整保留片段，列出省略原因；輸出預留也傳入實際 completion ceiling 與成本守衛。 |
| A9 回播 | 25 題合成來源與固定 evidence/time gold、兩輪共每路 50 samples；原始 FTS／semantic 與新 FTS／semantic／hybrid 同時比較。測試 fake provider、錯誤回傳、降級、預算、重建與舊快取映射；人工 golden 與真 API 驗收尚未完成，見下節。 |
| A10 整合 | `retrieve_evidence`／`answer_question` 是共用服務；#3 先透過同一 `search_index`／SearchFilter adapter 取歷史、排除目前 job 並使用完成 cutoff。本倉庫尚無 #6 Dashboard，未另建 Web／MCP 入口或循環相依。MiniMax keywords／answer、OpenRouter document／query embedding／rerank 各有原成本守衛操作名。 |

## TDD 與檢查

先寫新契約測試並執行，才修改實作。初次 RED 為 16 tests、18 failures／10 errors（含 subtests）：

```text
AssertionError: write connection
AssertionError: Lists differ: [[0, 1], [1, 0]] != [[1, 0], [0, 1]]
Ran 16 tests in 1.225s
FAILED (failures=18, errors=10)
```

審查發現的否定範圍、來源 pointer、錯誤 claim 型別、輸出上限、CLI action 旁路、完成日期與 audit 維度／hash 問題，也各自先留下 RED，再修正至 GREEN。兩位唯讀 reviewer 分別檢查檢索生命週期與 Ask／CLI；同一組程式只有主 worker 寫入。

可由 Git Bash 重跑的新契約與固定回播：

```bash
python -X utf8 -B -m unittest discover -s tests -p 'test_*retriev*.py' -v
```

結果：33 tests、33 passed、0 failed、exit 0。測試檔為 `test_grounded_retrieval.py` 與 `test_retrieval_evaluation.py`；另調整既有視覺回歸，明確測試舊索引查詢不寫入、明確 index 才遷移，以及舊向量可零請求沿用。

完整發布檢查：

```bash
.venv/Scripts/python.exe -X utf8 -B scripts/check_offline.py
pwsh -NoProfile -NonInteractive -File tests/test_windows_sync.ps1
```

初始完整套件 621 tests（595 passed、2 errors、24 skipped）；兩項 error 是宿主 Microsoft Store Python 在清空環境後啟動子程序的 WinError 87。改用忽略於 Git 的本機 venv（`--system-site-packages`，沒有下載依賴）後，原失敗檔 13 項通過／跳過 2 項，沒有修改生產程序控制程式。

最終完整套件為 654 tests（630 passed、0 failed、24 skipped），四個 Node widget suites 通過，整體 exit 0。Windows 同步 fixture 為 PASS、30 checks、exit 0；只使用測試暫存目錄中的本機 bare repository。`pipeline.py --self-test` 為 `self-test: OK`，三個 Linux GitOps shell 腳本的 `bash -n` 也為 exit 0。專案沒有設定獨立 lint、coverage ratchet、CRAP 或 mutation gate；未自行安裝。`check_offline.py` 包含完整 unittest、四個 Node widget suites 及 `git diff --check`。

## 固定回播數據

以下是合成資料的機制回歸，**不是人工語意支持率或真實模型準確率**。4 支影片、19 個 occurrences、17 種文字；模型 `deterministic-fake-9d`。來源標籤由 agent 撰寫，人工覆核未完成。基準只接受原本 category 參數，其餘新 filters 在新版生效，不能把改善全歸因於向量模型。

| 路徑 | Recall@5 | MRR | 無答案查詢正確空集合 | 引用結構／來源核對 |
|---|---:|---:|---:|---:|
| admitted base FTS | 0.9474 | 0.5877 | 0.5 | 原版沒有 occurrence 核對契約，未量測 |
| admitted base semantic | 1.0 | 0.7298 | 0.5 | 同上 |
| 新 FTS | 0.9474 | 0.9474 | 1.0 | 1.0 |
| 新 semantic | 1.0 | 1.0 | 1.0 | 1.0 |
| 新 hybrid | 1.0 | 1.0 | 1.0 | 1.0 |

一次本機量測：Windows 10 build 26300、Python 3.11.9、Intel64 Family 6 Model 186。每路 25 題×2 輪，first／warm 指第一輪與第二輪，未清空 OS cache；同時可能有其他離線套件工作，不是隔離 benchmark。

| 路徑 | 首輪／暖輪 median ms | Python peak bytes |
|---|---:|---:|
| baseline FTS | 9.144 / 10.439 | 11,606 |
| baseline semantic | 12.899 / 12.205 | 43,740 |
| FTS | 9.790 / 8.634 | 43,525 |
| semantic | 11.811 / 12.794 | 103,190 |
| hybrid | 24.076 / 16.825 | 143,142 |

該程序 peak RSS 77,172,736 bytes，為整個程序的累計高水位，各路非獨立程序，不能據此聲稱 RAM 降幅。假 provider 的真實請求／token／費用皆為 0；未量測真實 Ask token 或延遲。重跑會輸出 `RETRIEVAL_EVAL` JSON（含環境、index 數量、各路 metrics），延遲及 RSS 會隨機器負載改變。

## 已知界線／尚未做

- 人工 golden evidence／容許時間範圍覆核、人工語意支持率，以及真實模型的品質、token、latency／隔離 RSS 比較均**未驗證**。固定回播只使用合成來源與假 provider，不作實際準確率或加速宣稱。
- OpenRouter embeddings／rerank 已依官方 schema 驗證，但沒有帳戶授權 canary、價格確認或付費 fallback。Rerank 維持預設關閉；無資料與模型下載、全庫重嵌入或其他隱含付費。
- 回答驗證刻意只接受完整摘錄，會拒絕可能正確的自由改寫；`grounded` 表示通過此抽取與來源契約，不表示來源事實已由外部查證。跨不連續時間不合併成單一 range。
- 未更動分段策略、引入向量服務、安裝大型 ML 模型、建立 Dashboard 或新增 MCP tool。現有原始 artifacts 與舊向量保留；legacy 表的 backend／revision 必須由操作者明確確認才能 adoption。
- 未執行 push、PR、merge、部署或 issue 關閉；外部動作由 driver 處理。

官方契約：[OpenRouter embeddings](https://openrouter.ai/docs/api_reference/embeddings)、[OpenRouter rerank](https://openrouter.ai/docs/api/api-reference/rerank/submit-a-rerank-request)。模型／帳戶可用性不由文件存在推定。
