# Issue #11 — Evidence-backed Paper Cut / NLE Handoff（研究設計）

Research deliverable for `Reese-max/video-timeline-pipeline#11`。
**RESEARCH_RUNTIME_REQUIRED**：不能只靠 schema/unit test 關閉；需 OTIO
round-trip + FFmpeg source-range verification；無 NLE 時保持
`NLE_IMPORT_UNVERIFIED`。

## 1. `CutSpec` schema

```jsonc
{
  "cutSpecVersion": 1,
  "specHash": "sha256(...)",
  "source": { "videoPath": "...", "contentHash": "...", "fps": 29.97, "timebase": "30000/1001", "vfr": false },
  "segments": [
    {
      "segmentId": "seg_...",
      "sourceRange": { "startSec": 12.34, "endSec": 45.6 },
      "timelineRange": { "startSec": 0, "endSec": 33.26 },
      "evidenceRef": "transcript-span | scene-detect | manual",
      "reason": "cut-reason",
      "confidence": 0.9
    }
  ],
  "intendedDurationSec": 120.5
}
```

## 2. Interchange（MVP 只做一對可驗證 pair）

**CSV + OTIO**。Premiere/Resolve 專屬 EDL/FCPXML adapter 只有在能做
runtime import canary 後才加——OTIO/EDL/FCPXML 語意不同，不能用單一
schema test 代替真實 import。

## 3. Round-trip verification

1. CutSpec → OTIO export
2. OTIO parser 讀回 → 比對每個 `sourceRange` 與 `timelineRange`
3. FFmpeg `ffprobe`/`select` 對原影片驗證 source range 真的有內容
   （fps/timebase/VFR 一致）
4. 差異 → `degraded | failed` case，不假裝成功

## 4. `ImportCanaryReceipt`

```jsonc
{ "receiptId": "...", "specHash": "...", "exportFormat": "otio-0.17",
  "intendedRanges": [...], "importedRanges": [...],
  "mismatches": 0, "nleImport": "NLE_IMPORT_UNVERIFIED | verified-premiere | verified-resolve",
  "externalCalls": 2, "stillNeedsHumanFix": false }
```

## 5. Fixtures

| Fixture | 情境 |
|---|---|
| `clean-roundtrip` | OTIO 讀回完全等值 |
| `vfr-mismatch` | source 是 VFR → timebase 記錄，不當 CFR 計算 |
| `gap-segment` | 中間有空段 → 呈現為 gap 不補假內容 |
| `overlap-collision` | 兩 segment sourceRange 重疊 → 衝突標記 |
| `no-nle-available` | 無 NLE → `NLE_IMPORT_UNVERIFIED`，不假稱通過 |

## 6. 不做什麼

- 不先做 Premiere/Resolve adapter（無 runtime canary 前）。
- 不讓 evidenceRef 變成裝飾——每段必須可回溯到 transcript/scene-detect。
- 不用產品頁（Adobe/VEED/Descript）當驗收證據。
