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
      "evidenceRef": "cut-evidence/1:<job>:<analysisRevision>:transcript.json:/segments/0:<spanHash>",
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

1. CutSpec → CSV 或 OTIO export
2. 對應 CSV reader / official OTIO parser 讀回 → 比對每個 `sourceRange`、`timelineRange` 與 evidence identity
3. FFmpeg `ffprobe`/`select` 對原影片驗證 source range 真的有內容
   （fps/timebase/VFR 一致）
4. 差異 → `degraded | failed` case，不假裝成功

## 4. `CutReceipt`（實際落地欄位）

```jsonc
{ "schema": "cut-receipt/1", "receiptId": "sha256-prefix",
  "specHash": "...", "exportFormat": "otio|csv",
  "parser": "opentimelineio-0.18.1", "adapterVersion": "...",
  "expectedContentHash": "...", "observedContentHash": "...",
  "sourceFingerprint": "size:mtime_ns",
  "toleranceSec": 0.05,
  "segments": [{ "segmentId": "...", "status": "ok|degraded|failed",
    "intendedSourceRange": {...}, "importedSourceRange": {...},
    "deltaImportSec": 0.0015, "deltaFFmpegSec": 0.0,
    "ffprobe": { "frames": 45, "firstPtsSec": 0.0, "lastPtsSec": 1.468, "medianFrameDurSec": 0.033 },
    "observedCoverageSec": {...} }],
  "specIssues": [{ "code": "VFR_SOURCE", "severity": "degraded", "segmentId": null }],
  "mismatches": 0, "nleImport": "NLE_IMPORT_UNVERIFIED | verified-premiere | verified-resolve",
  "externalApiCalls": 0, "localSubprocessCalls": 3,
  "stillNeedsHumanFix": false,
  "status": "verified | failed | STALE_SOURCE | MISSING_SOURCE | SPEC_HASH_MISMATCH" }
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

## 7. 已落地的 spike（`cutspec.py`）

設計已實作為單檔模組 `cutspec.py`（stdlib + 可選 `opentimelineio`，
重用 `pipeline.probe_video / file_sha256 / file_fingerprint /
run_external / video_duration`），並附 CLI：

```sh
python3 cutspec.py probe   --video SRC
python3 cutspec.py build   --video SRC --segments segs.json [--out spec.json]
python3 cutspec.py preview --spec spec.json [--video SRC]
python3 cutspec.py export  --spec spec.json --format csv|otio [--full-paths]
python3 cutspec.py verify  --spec spec.json --video SRC \
    --export-file out.otio --format otio [--receipt receipt.json]
```

落地後相對 §1 的差異（刻意選擇，非偷渡）：

- `timelineRange` 不由使用者輸入，由 `build_cutspec` 依 segment 順序
  累加 source duration 推導——消除 input/derived 不一致的來源。
- segment 增加 `selection`（`manual|query_result|evidence_reel|suggested`）
  與 `note`；list 順序即 order，不另設會分叉的 `order` 欄。
- `specHash` 對 canonical payload 做 `sha256(sort_keys, compact
  separators)`。hash 輸入排除 `specHash` 自身、`videoPath` 與
  `fingerprint`——路徑與 mtime 是 locator 不是 revision，複製或搬移
  同一內容不得改變 spec identity（contentHash 才是 revision 依據）。
  非有限 float 在 hash 前 canonicalize 成 null。`verify_export` 會重算
  比對，不符即 `SPEC_HASH_MISMATCH`，因此偽造 specHash 會被抓到。
- `SourceInfo` 增加 `fingerprint`（pipeline 的 `size:mtime_ns`）與
  `durationSec`；`timebase` 採 ffprobe `avg_frame_rate` 的有理數字串。
- Validation 分三級：`reject`（MISSING_SOURCE / UNSUPPORTED_TIMEBASE /
  MISSING_RANGE / NEGATIVE_DURATION / OUT_OF_RANGE / MISSING_EVIDENCE /
  BAD_SELECTION / STALE_SOURCE / EMPTY_SPEC / MISSING|DUPLICATE_SEGMENT_ID）、
  `degraded`（UNTESTED_TIMEBASE / VFR_SOURCE / OVERLAP）、
  `info`（SOURCE_METADATA_DRIFT）。gap 只在 preview 呈現（SOURCE_GAP），
  不補假內容。
- Export **預設 redact** local path 只留 basename（`--full-paths`
  才保留完整路徑），`contentHash` 仍是身份依據；OTIO 內嵌的
  provenance payload 同步套用，且因 videoPath 不進 hash，redacted
  export 內嵌 spec 仍與 `specHash` 自洽。
- CLI 的 `build/export/verify --out` 皆為 tmp+rename 原子寫。

## 8. Runtime verification 結果（2026-09-17）

環境：Python 3.13.5、ffmpeg/ffprobe 7.1.5、opentimelineio 0.18.1、
pytest 9.1.1（Linux）。Frozen fixtures 在 `tests/fixtures/cutspec/`
（`manifest.json` 記錄每支的 sha256 與 probe 值，`regen_fixtures.sh`
可重現）。Receipt 原文在 `docs/research/issue-11-receipts/`。

| 情境 | fixture / export | receipt | 結果 |
|---|---|---|---|
| CFR 23.976 (`24000/1001`) | `cfr_23976.mp4` OTIO | `receipt-otio-cfr_23976.json` | verified, 0 mismatches |
| CFR 24 | `cfr_24.mp4` OTIO | `receipt-otio-cfr_24.json` | verified, 0 mismatches |
| CFR 25 | `cfr_25.mp4` OTIO | `receipt-otio-cfr_25.json` | verified, 0 mismatches |
| CFR 29.97 (`30000/1001`) | `cfr_2997.mp4` OTIO | `receipt-otio-cfr_2997.json` | verified, 0 mismatches |
| CFR 30 | `cfr_30.mp4` OTIO | `receipt-otio-cfr_30.json` | verified, 0 mismatches |
| CFR 30 | `cfr_30.mp4` CSV | `receipt-csv-cfr_30.json` | verified, 0 mismatches |
| VFR (r_frame_rate 30/1, avg 2880/119) | `vfr_30.mp4` OTIO | `receipt-otio-vfr_30.json` | verified, flags `VFR_SOURCE` + `UNTESTED_TIMEBASE` |
| source revision drift | cfr_30 spec → cfr_25 file | `receipt-stale-source.json` | `STALE_SOURCE`，不自動重綁 |
| missing source | 不存在檔案 | `receipt-missing-source.json` | `MISSING_SOURCE` |
| tampered export | clip `start_time` +200 frames | `receipt-tampered-export.json` | `failed`, 1 mismatch, `stillNeedsHumanFix=true` |
| forged specHash | 改 range 不重算 hash | `receipt-forged-spechash.json` | `SPEC_HASH_MISMATCH` |
| gap / overlap / reject previews | — | `preview-*.json` | gap 呈現不補假內容；overlap 標記；negative/out-of-range → rejected |

每張 receipt 記錄：`specHash`、`expectedContentHash` vs
`observedContentHash`、`toleranceSec`（預設 `max(1.5/fps, 0.15s for VFR)`）、
`parser`/`adapterVersion`（`opentimelineio-0.18.1`）、每段
`intendedSourceRange` vs `importedSourceRange`（`deltaImportSec` 全數
≈0.5–3ms，來自 frame-quantize 到 source rate）、`ffprobe` 觀測到的
coverage（`read_intervals` 會從前一個 seek point 解起，因此驗證語意是
「intended range ⊆ observed coverage」，不是邊界等值）、
`externalApiCalls: 0`、`localSubprocessCalls: 3`、
`nleImport: NLE_IMPORT_UNVERIFIED`（本環境無 Premiere/Resolve，不虛構
import canary）。

重現：生成 segments JSON → `build` → `export --format otio` → `verify`；
或直接 `pytest tests/test_cutspec.py`（33 tests，含 manifest 凍結檢查、
timebase 轉換 contract、無外部呼叫斷言、STALE_SOURCE 不重綁、
redaction、篡改偵測）。

## 9. Acceptance criteria 對照

| Issue 驗收 | 狀態 | 機制 / 證據 |
|---|---|---|
| frozen fixture 上 deterministic manifest/hash | ✅ | `compute_spec_hash` 排除 hash 欄位；`test_spec_hash_deterministic_and_self_excluding` |
| 100% entries 可追到 source job/hash/revision + start/end + evidence identity | ✅ strict resolver；legacy 未驗證 | `build-evidence` + `cut-evidence/1` companion 逐段解析實際 artifact/pointer/span；legacy 非空字串只標 `UNRESOLVED_EVIDENCE`、coverage=0 |
| out-of-range / negative / missing source / hash drift / unsupported timebase → reject 或 degraded，不 silent clamp | ✅ | `validate_cutspec` 三級 severity；對應 tests |
| preview/export 0 外部 API call、不讀 secrets、不改來源 | ✅ | `test_preview_and_export_make_no_external_calls` mock subprocess/urlopen 仍通過；`test_verify_does_not_modify_source` |
| CFR+VFR、23.976/24/25/29.97/30 fixture 驗證 conversion/rounding；未測 timebase 不宣稱 frame-accurate | ✅ | fixture matrix + `TESTED_TIMEBASES`；其他 → `UNTESTED_TIMEBASE` degraded；VFR → `VFR_SOURCE` degraded |
| 至少一種 interchange 真實 round-trip；無 NLE 用 OTIO parser + FFmpeg verification，保持 `NLE_IMPORT_UNVERIFIED` | ✅ | `opentimelineio 0.18.1` 真 parser 讀回 + ffprobe `read_intervals` coverage；全部 receipt `NLE_IMPORT_UNVERIFIED` |
| 匯出後 intended vs re-parsed 在 tolerance 內；失敗有 receipt | ✅ | `deltaImportSec`/`deltaFFmpegSec` per segment；tampered → failed receipt |
| source revision 變更 → `STALE_SOURCE`，需重新確認 | ✅ | `receipt-stale-source.json`；`validate_cutspec(current_source=...)` |
| AI candidate path 關閉時 manual/evidence export 完整可用 | ✅ | 本 spike 無 AI path；manual selection 全流程通過 |
| 不改寫既有 canonical artifacts；CutSpec 獨立版本化 | ✅ | CutSpec/companion 獨立輸出；pipeline 只為新分析保存 `source_sha256` 並驗證來源，舊 completed cache 拒絕時保持 manifest/evidence exact bytes，連續 retry 不重算付費分析 |

## 10. 已知誠實聲明 / 後續

- `read_intervals` 從 seek point 解碼，observed coverage 會往前延伸到
  keyframe——receipt 記錄的是「範圍被真實內容覆蓋」，不是逐 frame 對齊。
- VFR fixture 的 `avg_frame_rate`（2880/119）不在 TESTED_TIMEBASES，
  因此自動帶 `UNTESTED_TIMEBASE`；frame-accuracy 僅對測過的 CFR 宣稱。
- 無 NLE runtime → 所有 receipt `NLE_IMPORT_UNVERIFIED`；Premiere/Resolve
  adapter 仍待有合法環境時做 import canary。
- `#5 EvidenceOccurrence` 尚未上線。strict `build-evidence` 使用實際
  canonical artifact 的 JSON pointer + row hash；舊 `transcript:<file>:span-<i>`
  字串仍相容，但只標 `UNRESOLVED_EVIDENCE`，不聲稱已解析 provenance。
- Success metrics 中「人工 timecode 重輸入步驟」在本 spike 為 0
  （segments JSON → export），但相對手工基線的時間差未量測——
  不宣稱固定節省百分比。


## 11. Offline source job / span resolver（2026-10-05）

`cut_provenance.py` 補上先前 spike 缺少的實際 evidence resolution。
CutSpec v1、CSV 欄位與 OTIO 結構保持相容；strict 衍生物另外保存
`cut-evidence/1` companion。舊非空 evidenceRef 只證明字串保留，不能
證明 artifact 存在或 provenance coverage=100%。

支援兩種現有 canonical job：

- CLI schema-1 `manifest.json`：驗證 `job_dir` 與 artifact declarations，
  讀取同 job 的 `transcript.json`、`timeline.json`，要求持久化的
  `source_sha256` 等於目前媒體 bytes。新 pipeline 在分析前記錄此 SHA，
  完成前再驗證；舊 job 缺 hash 不會由目前媒體推測舊分析的來源。
- MCP version-2 ready manifest：要求 `job_id`、source fingerprint 與
  `analysis/result.json` 一致，驗證結果的 `source_sha256`，保存其
  `cache_signature` 並 hash 實際 result bytes。resolver 不載入 MCP server，
  不計算需讀 model/env 設定的 cache signature。

CLI 完整分析遇到 completed 舊 cache 缺 SHA 或 source drift 時，會在
任何 provider work 前拒絕 `STALE_SOURCE`，要求使用者明確 `--refresh`；
不會默默重新付費生成 evidence。拒絕發生在任何 canonical manifest write 之前，
保留舊 completed manifest 與 evidence 的 exact bytes，連續 retry 仍會拒絕；
不會因為首拒絕改成 failed 而讓下一次 retry bypass `--refresh`。local-only 不呼叫 provider。

Selection JSON 明確選取既有 row，**不提供 source range**：

```json
[
  {"artifact":"transcript","index":0,"selection":"manual","reason":"保留此句"},
  {"artifact":"timeline","index":1,"selection":"evidence_reel","note":"第二段"}
]
```

只能從 transcript `start/end` 或 speech timeline `timestamp/end` 推導
完整 span。summary、只有 timestamp 的 visual observation、缺 end、
字串/boolean/非有限時間、負值、超出來源與零時長都拒絕；不猜範圍、
不 clamp、目前不支援單句內部分選取。

```sh
python3 cutspec.py build-evidence --video SRC --job-dir ROOT/jobs/JOB \
  --selections selections.json --out spec.json --evidence-manifest evidence.json
python3 cutspec.py preview --spec spec.json --job-dir ROOT/jobs/JOB \
  --evidence-manifest evidence.json
python3 cutspec.py export --spec spec.json --format otio --out cut.otio \
  --job-dir ROOT/jobs/JOB --evidence-manifest evidence.json
python3 cutspec.py verify --spec spec.json --video SRC --format otio \
  --export-file cut.otio --receipt receipt.json --job-dir ROOT/jobs/JOB \
  --evidence-manifest evidence.json
```

Strict output 檔案須放在 canonical source job 之外，不可覆寫原媒體。
`build-evidence` 產生 companion 的主要欄位：

```jsonc
{
  "schema":"cut-evidence/1", "manifestHash":"sha256(canonical companion excluding itself)",
  "specHash":"...", "sourceJob":"12-hex job ID", "sourceContentHash":"source SHA256",
  "analysisRevision":"sha256(sourceJob/sourceHash/artifact identities/native revision)",
  "nativeAnalysisRevision":null, // MCP 為 cache_signature
  "artifacts":[{"file":"manifest.json","sha256":"exact file bytes SHA256"},
               {"file":"transcript.json","sha256":"..."},{"file":"timeline.json","sha256":"..."}],
  "entries":[{"segmentId":"seg-0","artifact":"transcript","index":0,
              "file":"transcript.json","pointer":"/segments/0","spanHash":"canonical row SHA256",
              "evidenceRef":"cut-evidence/1:<job>:<revision>:<file>:<pointer>:<spanHash>",
              "sourceRange":{"startSec":0.5,"endSec":1.5}}]
}
```

JSON hashes 使用 sorted compact ASCII JSON/UTF-8/SHA256，拒絕 nonfinite
values；artifact identities hash 原始檔案 bytes。EvidenceRef 包含 job、
analysis revision、pointer、span hash，因此 CutSpec hash 已綁定 evidence；
companion 再綁定 exact specHash。preview/export/verify 每次重讀 canonical
files，要求 companion hash、完整 entry order、實際 row/hash/range 相同。
缺 companion/artifact、跨 job path/symlink、source drift、analysis bytes
變更（即使 cache_signature 相同）或 forged span 全部 fail closed。

Receipt 增加 `exportHash`（實際 export UTF-8 bytes SHA256）與 `evidence`：
strict 成功包含 companion hash、job/revision、每段 resolved identity 及
`provenanceCoverage:1.0`；legacy 則為 `UNRESOLVED_EVIDENCE`、coverage=0。
`receiptId` 的既有 SHA256-prefix 計算包含這些欄位。這是完整 handoff 的
可重算 hash binding，不是第三方簽章，也不證明上游 ASR/模型內容為真。

新增測試在六支**原有、未修改** MP4 上跑 build-evidence/preview/export/
verify CLI、CSV + official OTIO 0.18.1 parser、實際 ffprobe，每段時間由
明確標示的 **synthetic frozen transcript/timeline span doubles** 提供。
此矩陣證明 resolver、hash binding 與媒體範圍 round-trip；不把 double
當真人逐字稿、模型準確率或人工操作基線。另有真 local-only pipeline
new-job SHA capture、analysis 期間 source drift 與 legacy/stale cache
provider-zero refusal 回歸。來源影音與既有 canonical artifacts 在
preview/export/verify 的前後 bytes 不變。

仍保持 `NLE_IMPORT_UNVERIFIED`；ffprobe 僅檢查解碼 coverage 包含 intended
range，VFR 不宣稱逐 frame boundary accuracy。未量測人工 timecode →
sequence 的完成時間基線，不宣稱省時百分比；historical `analyses/*`
不同格式與任意第三方/摘要 layout 未包含在此 resolver acceptance。


## 12. CSV interchange contract

CSV v1 使用 UTF-8、標準 `csv.writer` 的 comma delimiter / double-quote
escaping、`\n` record terminator；quoted 欄位可含逗號、雙引號、Unicode 與
換行，不能用 `splitlines` 當 CSV parser。讀回使用標準 `csv.DictReader`。
時間為來源／輸出 timeline 的 seconds，以 Python `repr` 保存數值；不在
CSV 字串階段做 frame snapping 或四捨五入。

固定欄位順序（`cutspec.CSV_COLUMNS`）：

```text
record,cutSpecVersion,specHash,videoPath,contentHash,fingerprint,durationSec,fps,timebase,vfr,segmentId,sourceStartSec,sourceEndSec,timelineStartSec,timelineEndSec,evidenceRef,selection,reason,confidence,note,intendedDurationSec
```

| Record | 必填／有意義欄位 | 語意 |
|---|---|---|
| 唯一首列 `source` | cutSpecVersion, specHash, videoPath, contentHash, fingerprint, durationSec, fps, timebase, vfr, intendedDurationSec | `vfr` 用 `0/1`；預設匯出 videoPath 僅 basename |
| 其後每列 `segment` | specHash, segmentId, sourceStartSec, sourceEndSec, timelineStartSec, timelineEndSec, evidenceRef, selection, reason, confidence, note | 順序為 clip order；每列 specHash 必須對應 source 首列 |

其餘欄位留空。reader 要求固定欄位集合且不可 duplicate；一列 source
必須先於 segments，拒絕多列 source、未知 record 與不同 specHash。
`verify_export` 比對讀回的完整 canonical source/segment/evidence payload，
再檢查 actual imported ranges 與 FFmpeg coverage。CSV 與 OTIO 都在六支
frozen CFR/VFR 媒體上實際 round-trip；變動 evidence/source/selection/
order/count/ranges 或壞 layout 會得到 failed receipt，不把 claimed hash
當成成功依據。
