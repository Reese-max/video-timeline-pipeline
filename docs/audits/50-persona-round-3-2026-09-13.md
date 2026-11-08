# 50-Persona Audit — Round 3

Date: 2026-09-13  
Protocol: `Reese-max/autodev-ng/docs/portfolio-audit/2026-09-06-50-persona-audit.md`  
Default branch: `main`  
Audited SHA: `dc421fef263ccc5b7321910304a9fa910c1a5c5d`

> Fixed 50-persona model simulation plus current default-branch repository evidence. Runtime claims require actual execution/CI/provider evidence; this round did not call a paid provider.

## Why this round was rerun

Round 2 found P2 #8 on the unbounded Bright Data Instagram discovery path. Since then, current `main` landed three product commits for notifications (#2), scoring (#3), and cost tracking / budget guards / provider health (#4). The same fixed personas were therefore rerun against the new default-branch product code, with additional emphasis on D04, H03/H05, I04/I05, and J04 around cost and period-boundary safety.

## Round 3 result

Status: **NEW P2 #18 + EXISTING P2 #8 STILL OPEN — NOT CLEAN**

### New P2 #18 — configured cost timezone is ignored by the daily budget period

The new cost tracker defines `_current_period_id()` as follows:

```python
def _current_period_id(period_type: str = "daily") -> str:
    tz_name = os.getenv("COST_TIMEZONE", "Asia/Taipei")
    now = datetime.datetime.now(datetime.timezone.utc)
    ...
```

`tz_name` is read but never applied. Daily/monthly period IDs therefore follow UTC dates regardless of the configured accounting timezone.

This is a safety-contract mismatch with the cost work's C3 design, which explicitly defines `COST_TIMEZONE=Asia/Taipei` as the proposed accounting boundary. With a finite `DAILY_BUDGET_USD` and `BUDGET_HARD_STOP=true`, an Asia/Taipei operator's bucket resets at 08:00 local time rather than local midnight. One local calendar day is split across two UTC buckets; reservations immediately before and after 08:00 can therefore be admitted against different daily buckets although they belong to the same configured local day.

The current unit tests verify arithmetic reservation blocking and zero external transport after a direct budget rejection, but they do not freeze time or cover timezone, local-midnight, UTC-boundary, or DST behavior.

Tracking: #18 — `[P2][50-persona audit] Honor COST_TIMEZONE for daily budget boundaries`.

Affected fixed personas: D04, H03, H05, I05, J04.

No real provider charge or overspend is claimed. This is a deterministic current-source/configuration defect and is classified P2 because the hard budget feature is opt-in, but its configured period boundary is part of its cost-safety contract once enabled.

### Existing P2 #8 remains reproducible

The Bright Data Instagram path remains unbounded by default after cloud opt-in. Current README still says that `--instagram-max-items` is optional and that the normal default has no result-count ceiling, while `--instagram-since` is a local post-return filter. Current source still accepts `max_items=None`; it only rejects non-positive values when a value is supplied.

The new general cost tracker does not by itself satisfy #8's acceptance requirement that a paid Instagram discovery cannot start without a finite provider-work bound. #8 remains open.

Affected fixed personas remain D04, C05, H05, J01, J03, I04, I05.

## Fixed 50-persona rerun summary

- D04: **FAIL** hard-budget accounting trust because the configured local-day boundary is not used; also still fails the unbounded Bright Data request scenario.
- H03/H05: **FAIL/PARTIAL** configuration and maintainer scenario because `COST_TIMEZONE` is accepted/read but operationally ignored and the repository has no current CI execution receipt.
- I04/I05: **FAIL/PARTIAL** provider-failure/retry/boundary scenarios because #8 is unresolved and period rollover behavior is not tested.
- J04: **FAIL** observability/audit scenario because reported period IDs do not establish the configured accounting timezone.
- Other persona improvements from the notification/scoring/cost commits are treated as source-level evidence only; this round did not execute real email, provider billing, long-running video processing, interrupted resume, or concurrency scenarios.

## Execution / runtime evidence boundary

GitHub Actions currently returns `total_count: 0` for `main`; no GitHub-hosted CI run exists for current SHA `dc421fef263ccc5b7321910304a9fa910c1a5c5d`.

The repository contains unit tests for cost arithmetic and budget rejection, but this round did not execute them through GitHub Actions or a provider canary. Therefore:

- no current-SHA test pass is claimed;
- no real provider budget enforcement is claimed;
- no real Bright Data/Groq/MiniMax/Apify charge behavior is claimed;
- no email delivery, provider health, interruption/resume, or concurrency runtime path is marked verified.

## Required acceptance before the next CLEAN count can begin

1. Resolve #18: apply the configured IANA timezone consistently to daily/monthly budget periods; fail explicitly on invalid timezone; add frozen-time tests for Asia/Taipei, local midnight, UTC-boundary, and a DST-observing zone.
2. Resolve #8: paid Bright Data discovery must fail closed without a finite provider-work/cost bound, including retry/resume behavior.
3. Obtain actual execution evidence for the relevant budget tests and the required provider/resume/concurrency paths; source/tests existing in the repository are not substituted for execution.
4. Re-run exactly the same fixed 50 personas after fixes land.
5. Require two consecutive current/recent rounds with no new P0/P1/P2 and required runtime evidence before marking CLEAN.

## CLEAN accounting

`video-timeline-pipeline` remains **NOT CLEAN**. Round 3 adds a new P2, so the consecutive no-new-P0/P1/P2 count is **0/2**.