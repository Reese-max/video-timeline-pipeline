# Video Timeline Pipeline — Product Board Audit

- Audit UTC: 2026-09-17T02:08:00Z
- Rules blob: `8167e10798071d2276addaff6b201c6b0e904a2a`
- Default HEAD inspected: `df0bbba4fea0d8e8197fe2f5e48625d4b005aaa1` (audit-only)
- Product baseline inspected: `dc421fef263ccc5b7321910304a9fa910c1a5c5d`
- Evidence level: repository source/issues/PRs and GitHub receipts; no paid provider call, real billing, browser, assistive-technology or production-data test
- Outcome: **INVEST / SIMPLIFY — NOT CLEAN, fixed A01–J05 streak remains 0/2**
- Scope: audit/triage only; no product code, configuration, secret, branch, merge, deploy, worker or paid action

## Discovery and deltas

The connected owner inventory returned 39 Reese-max repositories on 2026-09-17: 38 unarchived and one archived (`obsidian-vault`). `travel-planning-mcp` is unarchived but empty and is excluded from product-surface findings; absence of content is not treated as a defect. The remaining 37 unarchived, non-empty repositories stay in the fairness rotation. `video-timeline-pipeline` is owned by Reese-max, private, unarchived and non-empty. Its current default HEAD adds only the fixed-persona audit; it does not invalidate the last product SHA. All-state Issues and PRs, recent commits, relevant source/tests, and available workflow/status receipts were rechecked.

Three actionable root causes passed the quality gate:

| ID | Root cause | Kind / severity | Evidence | Tracking / decision |
|---|---|---|---|---|
| F1 | The cost reservation/usage functions have no production callers; supported provider transports can bypass the advertised hard stop and remain absent from reports | BUG / P2 / HIGH | SOURCE_CONFIRMED at product SHA; repository-wide caller search | [#4 reopened and narrowed](https://github.com/Reese-max/video-timeline-pipeline/issues/4); NEEDS_REVIEW; auto_implementation=false |
| F2 | PR #19 creates local period IDs but continues prefix-filtering UTC `created_at`; settled usage during local-day/UTC-day disagreement is counted as neither spent nor outstanding | BUG / P2 / HIGH | SOURCE_CONFIRMED, unresolved review | [#18](https://github.com/Reese-max/video-timeline-pipeline/issues/18) / [PR #19 thread](https://github.com/Reese-max/video-timeline-pipeline/pull/19#discussion_r4000498452); SKIPPED_LOCKED |
| F3 | PR #14 makes a positive Instagram cap mandatory, but persisted pre-change subscriptions store null and have no non-destructive update path, so every refresh fails | BUG / P2 / HIGH | SOURCE_CONFIRMED, unresolved review | [#8](https://github.com/Reese-max/video-timeline-pipeline/issues/8) / [PR #14 thread](https://github.com/Reese-max/video-timeline-pipeline/pull/14#discussion_r3988314793); SKIPPED_LOCKED |

PR heads `5b02434ad41d2f1c069e504f3dce824accb25801` and `efda1320e968df01bbd7ead8be4a3831b44806f2` had no Actions runs or commit statuses returned. Their source fixes are therefore not treated as tested, merged or effective. The CLI-help mismatch in PR #14 remains a lower-order symptom of F3 and is not a separate Issue.

## Problem proof and minimum scope

### F1 — disconnected cost guard

At the product baseline, `cost_tracker.py` defines `check_and_reserve_budget()` and `record_usage()`. Default-branch code search returns calls only from `tests/test_cost_tracker.py`; `pipeline.py` imports the module only for read-only cost/status commands. Groq transcription, MiniMax analysis, OpenRouter embeddings and other enabled transports therefore do not acquire or settle a reservation.

Affected users are local operators who deliberately opt into cloud processing and rely on `BUDGET_HARD_STOP=true` plus `DAILY_BUDGET_USD`. The supported workflow remains reachable, but the configured safety boundary is ineffective. The only current workaround is to disable cloud work or credentials.

The smallest safe change is to reuse the existing tracker at current transport boundaries: reserve before send; fail closed when hard-stop mode cannot produce a defensible bound; settle success/failure/unknown after send. It does not require another ledger, queue, service, hosted dashboard or provider expansion.

No charge or overspend is claimed. P2, not P0/P1, reflects opt-in reachability plus a source-confirmed safety-contract failure without live monetary evidence.

### F2 — local period versus UTC timestamp

PR #19 improves period-ID generation but not the query boundary. With Asia/Taipei, the first eight local hours use the next local date while new `created_at` values retain the previous UTC prefix. Once a reservation is settled, the row can disappear from both spent and outstanding calculations. The minimum correction is a local period’s UTC start/end range or a persisted accounting-period key. Silent fallback for an invalid IANA name also conflicts with #18 acceptance but is the same scope.

### F3 — existing subscription migration

PR #14 correctly rejects unbounded new paid Instagram discovery, but old persisted subscriptions have `instagram_max_items=null`. Refresh passes that null to the new guard; adding the author again is rejected, the update command cannot set the field, and removing the subscription deletes queued state. The minimum correction is a non-destructive migration or update command before enforcement. The separate help-text drift should be fixed in the same patch, not opened as another root cause.

## External alternatives and competitive posture

Sources were checked on 2026-09-17 UTC. Product claims are not independent outcome evidence.

| Alternative | Status / date | What it establishes | Product posture |
|---|---|---|---|
| [Adobe Premiere Text-Based Editing](https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/transcribe-video.html) | CONFIRMED; accessed 2026-09-17; page update date not exposed | Native transcription, transcript edits, speaker handling and paper edit inside a full NLE | MUST MATCH reliable transcript/timeline identity; DO NOT COPY the full editor surface |
| [Descript video editing](https://www.descript.com/video-editor) and [pricing](https://www.descript.com/pricing) | CONFIRMED; accessed 2026-09-17; update date UNKNOWN | Hosted text-first editing and tiered commercial workflow | SHOULD BE BETTER for local evidence traceability and bounded cloud effects; do not infer willingness-to-pay |
| [DaVinci Resolve](https://www.blackmagicdesign.com/products/davinciresolve) | CONFIRMED; accessed 2026-09-17 | Integrated editing, color, VFX, audio and collaboration | DIFFERENTIATOR is lightweight evidence-to-handoff, not competing as another NLE |
| [PySceneDetect](https://www.scenedetect.com/) / [0.7.1 changelog](https://www.scenedetect.com/changelog/) | CONFIRMED; 0.7.1 released 2026-07-21 | Free, open, embeddable shot-change detection with explicit algorithms | MUST MATCH reproducible local primitives; reuse rather than recreate mature scene detection |
| FFmpeg + local Whisper + spreadsheets/manual markers | LIKELY alternative workflow; no single product claim | Low cash cost and high operator effort; provenance depends on discipline | SHOULD BE BETTER on resumability, evidence receipts and bounded escalation |

Market conclusions:

- MUST MATCH: deterministic transcript/timeline identity, recoverability, explicit cloud consent, honest cost/runtime receipts.
- SHOULD BE BETTER: local-first inspection, evidence-linked handoff, bounded paid effects, replayable failure state.
- DIFFERENTIATOR: a compact evidence pipeline that produces reviewable artifacts for downstream editors.
- DO NOT COPY: full collaborative NLE, generative-media suite, hosted asset platform, marketplace, social publishing or unlimited background agents.

## Synthetic 50-persona exercise

This is model-based simulation, not independent experts, users, incidence, market share, revenue or priority evidence. R01–R30 retain regression journeys; X01–X20 explore adjacent constraints. It is separate from the fixed A01–J05 audit and cannot advance its CLEAN count.

| ID | Background / constraint | Goal and journey | Friction / result | Triage / recommendation / evidence |
|---|---|---|---|---|
| R01 | Solo researcher, laptop | ingest interview → transcript → evidence | cloud guard bypasses budget | P2; wire #4; source |
| R02 | Documentary editor | batch clips → search quote → handoff | no provider attempts in report | P2; #4; source |
| R03 | Podcast producer | long audio → Groq transcript | hard stop cannot block send | P2; #4; source |
| R04 | Student, fixed allowance | summarize lectures | configured daily limit is ineffective | P2; #4; source |
| R05 | NGO archivist | process sensitive oral history locally | local-only works; cloud must stay explicit | maintain; source |
| R06 | News researcher | query many videos | cross-day spend can disappear in PR fix | P2; #18; source |
| R07 | Taiwan operator | daily budget by local date | UTC prefix miscounts early hours | P2; #18; source |
| R08 | Traveling editor | changes timezone | invalid zone silently becomes UTC in PR | same #18; source |
| R09 | Cost-conscious creator | inspect budget status | report can be empty despite provider use | P2; #4; source |
| R10 | Automation operator | unattended RSS processing | opt-in cloud lacks connected guard | P2; #4; source |
| R11 | Existing Instagram subscriber | refresh saved author | PR #14 rejects stored null cap | P2; #8; source |
| R12 | Curator with queued items | add cap without losing queue | no non-destructive update path | P2; #8; source |
| R13 | New Instagram subscriber | add author with cap | explicit bound is safer | expected; PR source |
| R14 | CLI novice | follow `--help` | help still promises unlimited default | fold into #8; source |
| R15 | Windows operator | run local pipeline | runtime not exercised this round | pending runtime |
| R16 | macOS editor | build evidence pack | runtime not exercised | pending runtime |
| R17 | Linux CI maintainer | verify product SHA | no Actions/status receipt | validation gap; GitHub |
| R18 | Low-bandwidth user | resume interrupted download | no new regression proven | maintain; prior audit |
| R19 | Screen-reader user | use CLI reports | not tested with AT | evidence backlog |
| R20 | Color-blind reviewer | inspect Markdown evidence | no concrete defect reproduced | reject new issue |
| R21 | Multilingual interviewer | transcript non-English speech | provider runtime not tested | evidence backlog |
| R22 | Local-only privacy user | avoid external effects | supported boundary remains valuable | maintain |
| R23 | Editor with corrupt artifact | resume safely | no new failure reproduced | maintain |
| R24 | Batch operator | stop before paid effect | cloud opt-in alone is insufficient | P2; #4 |
| R25 | Auditor | reconcile request and cost | no production attempt IDs | P2; #4 |
| R26 | Support responder | explain failed provider job | reports miss unrecorded calls | P2; #4 |
| R27 | SRE | recover timeout | unknown-cost semantics not integrated | #4 acceptance |
| R28 | CFO-minded owner | cap monthly exposure | current code only daily and unwired | do not expand; fix root first |
| R29 | QA engineer | spy on transports under rejection | tests cover tracker, not transports | #4 acceptance |
| R30 | Security reviewer | check secrets/logs | no new secret exposure found | maintain / no issue |
| X01 | Premiere editor | import evidence-backed cut | NLE handoff research exists #11 | NEEDS_EVIDENCE |
| X02 | Resolve editor | use markers/EDL | narrow handoff, not full editor | LATER / #11 |
| X03 | Descript user | switch for local provenance | value plausible, not user proof | research only |
| X04 | Investigative journalist | verify quote occurrence | evidence linkage is differentiator | maintain |
| X05 | Academic lab | reproducible scene segmentation | PySceneDetect reuse is smaller | SHOULD BE BETTER |
| X06 | Legal reviewer | preserve source/timecode | no legal-grade guarantee claimed | evidence backlog |
| X07 | Agency editor | multi-user collaboration | out of scope | DON'T |
| X08 | Mobile creator | native phone editing | out of scope | DON'T |
| X09 | Social publisher | one-click posting | out of scope | DON'T |
| X10 | Archivist, air-gapped | all-local processing | preserve local path | NOW guard regression only |
| X11 | Creator with subscription credits | distinguish included vs paid | tracker cannot currently observe calls | #4 |
| X12 | Multi-provider operator | fail over models | automatic provider switching could bypass authority | DON'T until guard |
| X13 | Accessibility researcher | keyboard-only workflow | runtime evidence missing | NEEDS_EVIDENCE |
| X14 | Performance engineer | benchmark scene detection | no comparative runtime executed | NEEDS_EVIDENCE |
| X15 | Maintainer | upgrade PySceneDetect | current repo uses own primitives; no forced migration | LATER |
| X16 | Producer | approve creative cuts | agent output must remain proposal | maintain human approval |
| X17 | Compliance lead | audit content credentials | C2PA research #20 is bounded | NEEDS_EVIDENCE |
| X18 | Support lead | migrate legacy subscriptions | PR #14 needs repair path | P2 / #8 |
| X19 | QA lead | verify local-midnight boundary | PR #19 lacks CI receipt | P2 / #18 |
| X20 | Owner with three priorities | cost guard → period truth → safe IG migration | defers breadth | NOW |

Purely synthetic switching preference (50 simulated profiles): DaVinci Resolve 14, Premiere 12, this local pipeline 10, Descript 6, PySceneDetect/Whisper scripts 5, manual markers 3. These counts are illustrative only and were not used for severity or priority.

## Product board (model-simulated perspectives)

- CEO — If only three things: connect #4 to real transports; make #18 period accounting correct; ship #8 with legacy migration. Do not add an editor, mobile app or provider.
- CPO — Preserve local-first evidence and explicit cloud consent; competitor breadth is not the roadmap.
- CTO — Current tracker is a detached subsystem; integrate at existing boundaries before extending its schema.
- Staff/Principal Engineer — One guard adapter around existing sends is smaller than a new operations platform; PR #19 needs range queries, not string-prefix dates.
- UX Lead — A safety toggle that does not protect is worse than a clearly unavailable cloud action.
- UX Researcher — Synthetic personas show hypotheses; observe real setup, recovery and handoff before expanding.
- Growth — Trustworthy bounded cloud work is prerequisite to distribution; no funnel claim is available.
- CFO — P2 until monetary impact is observed, but do not market or rely on a cap that cannot intercept spend.
- Security/Privacy — Local-only must remain a first-class boundary; no secret or authority expansion.
- QA — Require transport spies, local-midnight cases and legacy subscription fixtures.
- SRE — Timeout must retain unknown exposure; deployment/CI absence blocks verification, not proof of outage.
- Accessibility — Keep CLI output structured and test keyboard/screen-reader paths later; no defect is asserted without execution.
- Support — Give existing subscribers a non-destructive cap migration and actionable failure message.

Material disagreement: Growth could prefer new integrations and UX could prefer hosted onboarding; CTO/CFO/Security reject both until effect authority, cost accounting and recovery are truthful.

## Red Team

1. “The tracker unit tests pass.” Counter: they call the tracker directly; production transports do not.
2. “PR #19 fixes timezone.” Counter: local IDs still filter UTC timestamp prefixes.
3. “Failing legacy subscriptions is safe.” Counter: fail-closed is appropriate, but deleting queue state is not an acceptable migration.
4. “Build a universal provider framework.” Rejected: current transport boundaries plus the existing tracker are sufficient for the root cause.
5. “A competitor has collaboration/mobile/generative media.” Rejected: difference is not a reproduced product defect.
6. “Raise #4 to P0/P1.” Rejected without a real charge, uncontrolled request receipt or material monetary incident.
7. “Run live paid canaries.” Not authorized; deterministic transport spies and isolated fixtures come first.
8. “Close based on diff/merge.” Rejected; default-branch product SHA and the original scenario need rerun receipts.

## Decision memo and sequencing

Service: solo researchers, creators and editors who need a local, resumable, evidence-linked path from raw video to searchable timeline and downstream handoff, with explicit authority over cloud effects.

Choice: compete on evidence truth, recoverability and bounded external effects, not editor breadth. PySceneDetect/FFmpeg/local models are complements; Premiere, Resolve and Descript are downstream or alternative workflows.

- NOW: #4 provider-bound guard; #18 correct accounting period; #8 safe bounded discovery including legacy migration.
- NEXT: #11 narrow NLE handoff and #20 C2PA research only after the safety baseline.
- LATER: measured accessibility/performance and authorized provider canaries.
- DON'T: full NLE, hosted collaboration SaaS, mobile app, social publishing, unlimited agents, automatic top-up/failover or new providers.

Recommendation: **INVEST / SIMPLIFY**. No archive/merge action is authorized.

## Writes, mapping and limits

- Findings: 3 (P0 0 / P1 0 / P2 3 / P3 0)
- Reopened Issues: 1 (#4); title/severity narrowed from unsupported P0 to P2
- New Issues: 0
- Updated active Issues/PR scopes: 0
- Duplicate avoided: 3 (F2→#18/PR #19, F3→#8/PR #14, help drift→F3)
- SKIPPED_LOCKED: 2 active PR scopes (#18/#19 and #8/#14)
- Rejected/deferred standalone proposals: 12
- Verified fixed: 0
- Finding mapping: 3/3
- Issue/report write blocked: 0 at authoring time
- Runtime pending: real provider/billing, transport canaries, OS/browser/AT, current-SHA CI
- Fixed A01–J05: NOT CLEAN, 0/2; this product-board simulation does not advance it
- Fairness cursor after this product: `ai-novel-workstation`
