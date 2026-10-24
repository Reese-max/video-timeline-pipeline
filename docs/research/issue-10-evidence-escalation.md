# Issue #10 — 整片 MP4 基準下的局部證據重看研究

## 2026-10-24 修訂：等待素材與人工標註，尚無產品決策

本輪整合 PR #16 的設計文件，並修正已過時的比較基準。現行管線已移除固定抽幀分析；baseline 是 MiniMax 直接讀取整片 MP4，長片依 token 預算分段、保留原 fps/detail，且每次最多一次有界的畫面補漏。補漏後仍有超過 30 秒的間隔會標示未完成。因此，不能以「30 秒抽樣漏掉短畫面」的舊合成例子宣稱現行產品需要 escalation，也不能以目前已存在的 gap fill 冒充研究增益。

下方 2026-10-20 的 FFmpeg spike 是來源 PR 的歷史紀錄，**本輪未重現**，不是現行 baseline，也不是 BUILD/NARROW/REJECT 的有效品質結論。現行決策為 `NEEDS_EVIDENCE`；尚未實作 escalation selector，功能維持關閉。沒有執行付費 API、下載研究影片或重算使用者資料。

### 可執行的配對研究流程

1. 凍結 16 支可合法使用、具可讀來源的原片，逐支記錄 source SHA-256、容器／影像長度、fps、尺寸與來源授權。規劃 UI／程式操作、投影片／密集文字、短暫動作／物件、混合語音與畫面四組各 4 支；各組包含正常及故障／無答案情境。這是素材分層計畫，現階段尚未取得這 16 支影片。
2. 每片由人工先標出至少 2 題可核對的視覺任務與 1 題無答案或純語音控制，保存原片時間區間、逐字畫面答案及容許誤差，然後凍結 task set。標註者先看原片，不能以模型回應反推 gold。另由第二人覆核樣本與分歧。
3. baseline 固定當次 commit、MiniMax model／endpoint contract、原片 hash、fps/detail、chunk 設定、輸出上限及補漏政策。使用現行整片路徑一次產生 canonical evidence，保存 response hash、receipt、pending gap、每次成本與 elapsed。保留所有失敗，未知費用不能補零。
4. 在相同 canonical evidence 上配對比較「只使用 baseline 回答」與「明確視覺問題＋可信時間候選＋一次局部重看」。局部候選限 1–10 秒、最多 20 張或等價的已核准短片輸入、最多 1 次 attempt；沿用成本守衛與取消／deadline。不允許無時間候選的全片盲掃，不改寫 baseline，不啟用產品 fallback。
5. 先只驗證本機 extractor／manifest／範圍與 hash 的機制；影格可抽取不代表語意可讀。真模型配對研究必須先有明確 provider 設定與支出授權，逐 attempt 留帳；未授權不送出。控制組無額外 provider request。
6. 用同一份人工 gold 計算每片／每任務 precision、recall、時間對齊誤差、無答案正確拒絕、schema／provider failure、額外 frames／bytes／latency／requests／actual與unknown費用。對影片做 paired bootstrap，列原始 counts 與信賴區間，不把同一影片多題當成獨立影片。失敗及退出亦留在 denominator。
7. 凍結結果後才寫 BUILD/NARROW/REJECT：只有有界重看在預先定義的視覺子類有可重現增益、無答案／純語音不退步、引用及範圍全數可核對，且支出／延遲上限通過時，才考慮 NARROW 或 BUILD。否則保留 baseline 並 REJECT；不能用 schema 通過率代替語意品質。

研究開始前還須由維護者寫下可接受的 precision／recall 退步幅度與增益、延遲及單題成本上限；本輪沒有假設使用者的商業取捨，不能事後依結果調整門檻。

### 本輪交付與外部缺口

- 已完成：把比較基準改為整片 MP4、區分現行 gap fill 與額外 escalation、整理 16 片配對分層及凍結／量測流程，保留歷史設計可追溯性。
- 未完成：實際 12–20 支原片及授權、人工 gold／覆核、真 provider 研究預算與執行收據，以及依凍結門檻作出的產品決策。離線工程回歸不會消除這些研究缺口。

---

## 歷史來源 PR #16（以下原文已由上述基準修訂取代）

# Issue #10 — 基線抽樣與按需局部加密重看

Research spike for `Reese-max/video-timeline-pipeline#10`。決策：**NARROW**；保留固定 baseline，先只做有界、本機、可產生 manifest 的候選 spike。此文件記錄的 runtime 僅驗證 FFmpeg 局部抽取，不代表視覺語意正確率、provider 成本或產品驗收。

## 問題與適用範圍

固定抽樣（約每 30 秒一張）建立可重播的全片 baseline。使用者詢問短暫畫面、claim verification 或分數依據，且現有 baseline 不足以回答時，局部重看可能比提高整片 frame budget 更合適。只有明確可定位的視覺問題才考慮 escalation；純語音問題、沒有時間候選、或無法說明 evidence gap 時回 `insufficient_visual_evidence`。不以模型常識填補缺證。

維持兩階段且分開留痕：

```text
stable baseline ingest → task-specific evidence gap → bounded request
→ local rescan/zoom → evidence manifest → claim/retrieval verification
```

Baseline 負責一般 timeline、summary、coverage 和 cache；escalation 不回寫 canonical baseline。新證據只掛在該次 query 的附加 manifest，後續重跑可重現但不會悄悄改寫舊結果。功能預設 OFF；建立 request 前不增加雲端呼叫。

## Request 與隱私契約

Request 在記憶體中包含 query/claim 以選擇候選區間；落盤紀錄只保存正規化文字的 SHA-256，不保存原文、prompt、使用者身份或可逆的 query token。Hash 僅供去重，不宣稱匿名化。時間候選必須由既有時間戳或使用者明確指定，不能由全片盲掃猜出來。

```json
{
  "schema": "visual-escalation-request/1",
  "requestId": "sha256(sourceHash|queryHash|interval|policyVersion)",
  "queryHash": "sha256(normalized query)",
  "claimHash": "sha256(normalized claim)",
  "sourceHash": "sha256(source bytes)",
  "baselineEvidenceIds": ["..."],
  "targetIntervalSec": {"start": 751, "end": 760},
  "selectionReason": "claim timestamp is between baseline samples",
  "maxFrames": 20,
  "maxAttempts": 1,
  "maxProviderCostUsd": 0,
  "policyVersion": "local-rescan/1"
}
```

請求檢查：來源 hash 必須與 baseline 相同；區間須在片長內且長度 1–10 秒；frame cap 為 20；本機模式 attempts=1、provider cost=0。任何上限缺值、超界、來源改變或候選時間不足都 fail closed，不做全片 fallback。相同 source/query hash/interval/policy 的結果可重用；不同 prompt/provider/model contract 不共用結果。Provider mode 未納入本 spike，日後必須 opt-in，且沿用專案 cost guard；沒有供應商端上限時不可聲稱 hard cost cap。

## Receipt / evidence manifest

`request`（選哪裡）與 `receipt`（實際看了什麼）分開保存。Receipt 至少須有：

```json
{
  "schema": "visual-escalation-receipt/1",
  "requestId": "...",
  "sourceHash": "...",
  "baselineEvidenceIds": ["..."],
  "escalationEvidenceIds": ["..."],
  "targetIntervalSec": {"start": 751, "end": 760},
  "frames": [
    {"timestampSec": 754.0, "imageSha256": "...", "selectedBecause": "1s grid inside target"}
  ],
  "provider": "local-ffmpeg",
  "model": null,
  "promptVersion": null,
  "parserContract": "image-manifest/1",
  "attempts": 1,
  "framesScanned": 1,
  "providerCostUsd": 0,
  "localElapsedMs": 0,
  "status": "complete | insufficient_visual_evidence | failed"
}
```

影格 hash 對實際送檢的 image bytes 計算；timestamp 使用來源影片秒數。局部重掃失敗、空結果、解析失敗或 hash 不一致時留下失敗 receipt，不能回寫 baseline，也不能產生 claim 已驗證的狀態。Provider 加入時需同時記錄實際 provider/model、prompt hash/version、schema/parser contract、回應 hash、每次 attempt 的成本與延遲；未取得成功的 typed response 仍為 `insufficient_visual_evidence`。

## 本機 runtime spike（2026-10-20）

### Frozen protocol

- Fixture：`tests/fixtures/cutspec/cfr_24.mp4`，4.0 秒 CFR 合成時間碼 fixture；不是有標註事件的視覺 benchmark。
- Baseline：FFmpeg `fps=1/30`，frame 上限 40。
- 局部重看：同一來源，`-ss 1 -to 3`、`fps=1`、frame 上限 20。
- 輸出統一為 `format=rgb24` PNG；無網路、provider、model、secrets；只比較候選影格數與輸出 bytes，不計語意命中率。
- Runtime command：

```powershell
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 tests/fixtures/cutspec/cfr_24.mp4
ffmpeg -v error -i tests/fixtures/cutspec/cfr_24.mp4 -vf "fps=1/30,format=rgb24" -frames:v 40 $BASE/frame_%03d.png
ffmpeg -v error -ss 1 -to 3 -i tests/fixtures/cutspec/cfr_24.mp4 -vf "fps=1,format=rgb24" -frames:v 20 $LOCAL/frame_%03d.png
```

### Observed results

| Path | Exit | Frames | Image bytes | Interpretation |
|---|---:|---:|---:|---|
| 30 秒 baseline | 0 | 0 | 0 | 4 秒 fixture 短於 sampling interval；沒有覆蓋 1–3 秒區間 |
| 1–3 秒 local rescan | 0 | 2 | 5,301 | 局部抽取成功，兩張影格；沒有做語意判讀 |

第一個 JPEG 編碼嘗試因 fixture pixel format / encoder 不相容而失敗（exit `-22`）；以統一 PNG + `format=rgb24` 重跑成功。PNG baseline 的 0 張是有效觀察，不把它當抽取故障。無可比的標註事件、無模型 response，故 precision/recall、延遲改善與付費節省皆 **未驗證**。此例只證明局部抽取能取得 baseline 沒覆蓋的時間範圍，不能證明 escalation 有助答題。

## 規則怎麼驗收

1. **Interval/frame bound**：要求區間長 1–10 秒，且 `framesScanned <= min(20, ceil(durationSec * fps))`；對負值、超片長、空區間及第 21 張拒絕。以輸出 manifest 的 timestamp 和 image hash 核對每張來源影格。
2. **Attempt/cost bound**：本機 extractor 僅呼叫一次、provider cost 固定為 0；provider path 未實作，不得用估算值冒充供應商端上限。若將來 opt-in provider，逐 attempt 記錄 cost guard reservation/receipt，缺少可執行上限時 strict 模式拒絕。
3. **Dedup**：key 是 source hash + query hash + interval + policy version；同 key 重放應重用相同 manifest，不再呼叫 provider。Prompt/model/parser 改版需換 cache key。
4. **Audio-only / no candidate**：以 query route classification 測試純語音問題不建立 request；無可信時間候選回 `insufficient_visual_evidence`，不得退化成整片掃描。
5. **Immutable baseline**：執行前後比較 baseline manifest hash；新增 escalation evidence ID 只能在 query receipt 中，baseline hash 必須相同。

以上 1–5 是驗收契約，不是本次已通過的產品測試。這次 runtime 只做了上述兩條 FFmpeg command；專案尚無 escalation selector/receipt implementation，故其餘行為待落地測試。

## 社群與供應商訊號的權重

PaperEdits 的 6 支 synthetic clips / 5 組有效 matched pairs 報告 agentic 找到 18/20、static 找到 15/20，但 static planning time 約快 45%，且樣本沒有 human viewing panel，另有 agentic JSON contract failure。這是小樣本且具產品關聯的 anecdotal signal，只支持先 shadow evaluate，不能推論一般影片表現。

Lirovo 的產品頁描述 transcript 與 on-screen evidence 對齊、scene detection/frame dedup、typed JSON 和 exact-second evidence。這些是相關產品主張，可供設計參考；不是本 repo 能力或獨立效能證據。本次依使用者提供的來源摘要記錄，未連網查證。

## 結論與下一道證據門檻

**NARROW**：值得測的候選類型是「使用者已提出視覺 claim，且 transcript/既有 baseline 給出可信的窄時間候選，但 baseline frame 恰未覆蓋」；例如短暫畫面文字、物件出現/消失或動作瞬間。純語音問答、沒有時間定位的廣泛探索、只想重做 summary 都不應 escalation。現在只保留離線局部掃描與 manifest 契約的 spike，尚不足以決定建置可回答使用者的 escalation。

升級為 `BUILD_LOCAL_ESCALATION` 前，需用 frozen、具人工標註的多影片 task set 做 paired baseline/local evaluation；同時量測 event recall、precision、JSON/schema failure、每題 frame 數、bytes、local latency、provider attempts/cost，保留所有失敗樣本，並確認 baseline hash 不變。若評估 provider mode，須同 protocol、model contract 和 task set，先 shadow、opt-in、cost guarded；不能以本地抽幀結果推論 provider 優劣。

## 不做什麼

- 不把 agentic/provider mode 取代固定 baseline，不為一般 ingest 新增 paid calls。
- 不做 query-conditioned 全片重掃或無界 retry；沒有可信候選就停止。
- 不把 request selection 和 extraction result 合併成單一不可稽核事件。
- 不把合成時間碼 fixture、社群小樣本或供應商產品敘述當成準確率結論。
