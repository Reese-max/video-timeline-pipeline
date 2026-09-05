# 50-Persona Audit — Round 1

Date: 2026-09-06
Protocol: `Reese-max/autodev-ng/docs/portfolio-audit/2026-09-06-50-persona-audit.md`

> Fixed 50-persona model simulation plus repository evidence review; not 50 human participants.

## Round 1 result

Status: **STATIC PASS / FRESH RUNTIME EVIDENCE PENDING — NOT CLEAN**

No new reproducible P0/P1/P2 finding was confirmed from the static evidence reviewed this round.

## Evidence reviewed

- README defines clear source/work/output boundaries and keeps jobs/batches/RSS/local DB/secrets out of Git.
- Cloud work is opt-in for batches via `--allow-batch-cloud`; documentation explicitly warns MiniMax Token Plan overage may fall through to paid credits.
- Intermediate JSON/Markdown writes use temporary replacement and incomplete extraction artifacts are rebuilt rather than reused.
- Cache keys bind source fingerprints, models/settings/prompts to reduce duplicate spend while preserving invalidation.
- Facebook/Instagram/Apify integrations document access limitations, token-in-header rules and provider-cost boundaries instead of claiming complete scraping.
- README records a prior 2026-09-01 real Groq/MiniMax synthetic-video test, but this is historical evidence and is not treated as a fresh Round 1 run.

## Fixed-persona scenarios still requiring fresh runtime evidence

- I02/I05: interrupted downloads/transcription/vision/summary writes and resume behavior.
- I04: Groq/MiniMax/yt-dlp/FFmpeg/provider 429/5xx/timeout and partial-success paths.
- D04/J48: batch budget exhaustion, provider overage prevention and long-run cache correctness.
- J02: concurrent batches/jobs using shared roots/index state.
- D05/J49: token/log redaction and source-provenance integrity across external discovery providers.
- H05: clean setup with optional dependencies and a no-network dry-run.

## CLEAN gate

1. Execute self-test/unit suite plus fresh synthetic-video end-to-end and interrupted-resume fixtures.
2. Verify provider-cost gates before any batch cloud call and test overage/timeout failure modes.
3. Test concurrent jobs/index updates and source-deletion/stale-feed handling.
4. Re-run fixed personas and require two consecutive rounds with no new P0/P1/P2 before CLEAN.

## Runtime status

**Pending for Round 1.** Historical real-provider performance evidence exists in README, but no new provider call was made during this audit.

---

# Round 2 continuation — 2026-09-06

> Static/code/documentation review only. No claim is made that a Bright Data charge actually occurred.

## Result

Status: **NOT CLEAN — NEW P2**

### P2 — Bright Data Instagram discovery is cloud-opt-in but unbounded by default

The Instagram author path correctly requires `--allow-batch-cloud`, but `--instagram-max-items` defaults to `None` and both CLI help and README state that the normal default has no result-count ceiling. The documented `--instagram-since 365d` filter is applied locally to returned publication times, so it is not a provider-side billing bound.

This differs from the Apify Facebook path, which has an explicit `APIFY_MAX_TOTAL_CHARGE_USD` value and sends `maxTotalChargeUsd` to the provider. For Bright Data, an operator who has opted into cloud use can still start a provider-consuming discovery with no finite work/cost ceiling.

Tracking: `#8 — [P2][50-persona audit] Bound Bright Data Instagram discovery cost instead of defaulting to an unlimited result set`.

Affected fixed personas: D04, C05, H05, J01, J03, I04, I05.

## Regression requirement

With a valid Bright Data token, invoke Instagram discovery with `--allow-batch-cloud` but without a finite result/cost budget. The process must stop before the outbound provider request. A bounded run must report and enforce its maximum provider work without exposing the API token, and retry/resume must not silently create a second billable snapshot.

## Updated CLEAN gate

Issue #8 must be resolved or explicitly justified `not_planned`; then run the fresh provider/resume/concurrency scenarios from Round 1 and require two consecutive fixed-persona rounds without new P0/P1/P2 before CLEAN.