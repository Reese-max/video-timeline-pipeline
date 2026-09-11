# Issue #10 — 基線抽樣＋按需局部加密重看（Agentic Visual Evidence Escalation）

Research deliverable for `Reese-max/video-timeline-pipeline#10`。
**RESEARCH_RUNTIME_REQUIRED**：需實跑 baseline vs local escalation。

## 1. 問題陳述

現行視覺 coverage 是固定抽樣（~每 30 秒 1 張）；當 query/claim 指出某
1–5 秒局部範圍需要更強視覺證據時，只能整支影片提高 frame budget 或
人工回原片找——貴且慢。

## 2. 二階段流程

```
stable baseline ingest → task-specific evidence gap →
bounded escalation request → local rescan/zoom →
evidence manifest → claim/retrieval verification
```

**不取代 baseline**——baseline 仍是 deterministic、可重播的第一層；
escalation 只在 query 明確指出範圍時觸發。

## 3. `EscalationRequest` / `EscalationReceipt`

```jsonc
{
  "escalationId": "esc_<ulid>",
  "query": "...", "claimRef": "...",
  "targetInterval": { "startSec": 120, "endSec": 150 },   // 有界，非全片
  "maxFrames": 20, "maxCostUsd": 0.01, "maxAttempts": 2,
  "sourceHash": "...", "baselineCoverage": "1 frame / 30s"
}

// receipt
{ "escalationId": "...", "framesScanned": 18, "costUsd": 0.004,
  "latencyMs": 3200, "evidenceFound": true,
  "evidenceWindows": [{"start":131,"end":134,"relevance":"high"}],
  "cacheKey": "query+interval+sourceHash" }
```

## 4. 規則（issue 驗收）

- 每次 escalation 有 interval/frame/cost/attempt **上限**——不讓 agent
  無界重看影片。
- query/claim 無足夠時間候選 → `INSUFFICIENT` 或明確 bounded fallback；
  不對全片無界加密掃描。
- query/interval/source hash 去重 cache——重複 Ask 不重複付費。
- 只有音訊可答的 query 不做 vision escalation（不浪費）。

## 5. Provider mode（若測 Gemini agentic）

同一 frozen protocol / model contract / task set；vendor mode 的
JSON/schema failure、cost、latency 一併記錄，與 local 並列比較。

## 6. 結論輸出（必填）

`BUILD_LOCAL_ESCALATION | BUILD_PROVIDER_OPTION | NARROW | REJECT`——
明確寫哪些 query 類型才值得 escalation。

## 7. 不做什麼

- 不讓 escalation 取代 baseline（query-conditioned 天生有 blind spot：
  沒被提示到的 evidence 仍會漏——所以只做二階段）。
- 不在 cache/去重機制建立前上線多次 Ask escalation（成本累積風險）。
