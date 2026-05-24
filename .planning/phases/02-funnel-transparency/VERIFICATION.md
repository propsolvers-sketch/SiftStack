---
phase: 02-funnel-transparency
verified: 2026-05-24T19:00:00Z
status: partial
score: 3/4 ROADMAP success criteria fully verified (SC-4 is documented PARTIAL per CONTEXT.md D-04 deferral)
overrides_applied: 0
re_verification: null
deferred:
  - truth: "SC-4 — Yellow warning fires when 2Captcha drops 99%→80% (or any service degrades)"
    addressed_in: "Phase 5+"
    evidence: "CONTEXT.md D-04 + 02-CONTEXT.md `<deferred>` block: 'Alert thresholds + paging — Phase 5 or later. Once we have rolling baselines, we can add yellow/red bands. Not in Phase 2.' ROADMAP success criterion #4 aligns: Phase 2 emits the numbers, humans interpret. Data substrate (per-run rate + 7-day rolling baseline) IS shipped — the operator can already see the divergence visually."
  - truth: "apn_probate scrape_all rate_tracker plumbing — 2Captcha rate currently reads n/a today in pure-apn_probate runs"
    addressed_in: "(known incremental gap, not a phase-blocking gap)"
    evidence: "Documented in 02-04 SUMMARY explicitly: 'apn_probate's pure-CLI runs will continue reading n/a today until that orchestrator instantiates a tracker and passes it into scrape_all itself.' main.py daily flow DOES pass rate_tracker into scrape_all (the canonical scheduled path), so the 2Captcha rate IS populated for the daily scheduled run that matters."
---

# Phase 2: Funnel Transparency — Verification Report

**Phase Goal (from ROADMAP):** Make pipeline behavior observable on every run so quality regressions surface immediately, not 24-48h later when "fewer records produced today" gets noticed.

**Verified:** 2026-05-24
**Status:** partial (SC-4 documented as deferred per CONTEXT.md — not a fail; substrate shipped, alert thresholds explicitly out of scope)
**Re-verification:** No — initial verification

---

## Goal Achievement

### ROADMAP Success Criteria (the contract)

| # | Success Criterion | Status | Evidence |
|---|---|---|---|
| SC-1 | Every pipeline run logs and Slacks the full funnel — drop counts at every gate | VERIFIED | All 6 pipelines instantiate `FunnelCounter("<name>", gates=[...])` and POST a 3-block Slack payload (summary + funnel + service-rates) via `_send_blocks_webhook` in one HTTP call. Gate sequences match CONTEXT.md D-01: main_daily=10, apn_probate=6, pre_probate=9, benchmark=6, tax_distress=5, code_violation=3. Each pipeline emits `logger.info("Funnel (%s): %s", ...)` at end-of-run (D-04 terminal mirror), so funnel data lands on terminal regardless of `--notify-slack`. |
| SC-2 | User can audit any per-run conversion drop visually in the Slack summary without re-running | VERIFIED | `build_funnel_block(pipeline_name, gate_counts)` renders the OrderedDict gate sequence as `• gate_name: count` lines in insertion order. Pre-seeded gates always render even when count==0 (zero is signal). Operator can scan the Slack block top-to-bottom and identify where conversion dropped (e.g. "all dropped at llm_extracted: 0"). |
| SC-3 | Per-service success-rate metric appears in daily Slack for 2Captcha / Smarty / Tracerfy / LLM | VERIFIED | `build_service_rates_block(per_run_rates, rolling_rates)` renders 4 fixed-order lines: "2Captcha: 100% today \| 99% 7-day" etc. All 4 services instrumented per D-04 (captcha_solver.py, address_standardizer.py, llm_client.py + llm_parser.py, tracerfy_skip_tracer.py). `_RATE_DISPLAY_ORDER` constant pins service order. `None` distinguishes "no calls" (`n/a today`) from "all failed" (`0% today`); rolling baseline `None` renders as `— 7-day` for clean operator scan. |
| SC-4 | Yellow warning fires when 2Captcha drops 99%→80% (or any service degrades) — alert thresholds | PARTIAL (DEFERRED — documented) | **Data emitted, alert thresholds deferred to Phase 5+.** CONTEXT.md D-04 explicitly defers: "Alert thresholds: No alerting in Phase 2. Just emit numbers. Humans decide what's bad." 02-CONTEXT.md `<deferred>` block reaffirms: "Alert thresholds + paging — Phase 5 or later. Once we have rolling baselines, we can add yellow/red bands." The substrate (per-run rate + 7-day rolling baseline visible side-by-side in same Slack block) is shipped, so an operator can visually identify the divergence today. Programmatic yellow/red-band emission is NOT in Phase 2 scope. |

**Score:** 3/4 ROADMAP SCs fully verified; SC-4 is documented PARTIAL (data substrate shipped, alert thresholds explicitly deferred to a later phase per CONTEXT.md D-04 + ROADMAP framing).

---

## Required Artifacts (Wave 1 foundation)

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/observability.py` | FunnelCounter + ServiceRateTracker + load/save + summary | VERIFIED | 344 lines. All 7 public symbols present (FunnelCounter, ServiceRateTracker, TRACKED_SERVICES, STATE_FILE, load_rolling_rates, save_rolling_rates, rolling_rates_summary). Zero slack_notifier imports (one-way dependency direction enforced). |
| `src/slack_notifier.py` | build_funnel_block + build_service_rates_block + _send_blocks_webhook | VERIFIED | 493 lines. 5 public funcs visible at expected line numbers: `_send_webhook(25)`, `send_slack_notification(286)`, `build_funnel_block(369)`, `build_service_rates_block(402)`, `_send_blocks_webhook(459)`. Existing functions (send_slack_notification + _send_webhook) untouched per Wave 1 contract. |
| `tests/unit/test_observability_counters.py` | FunnelCounter + ServiceRateTracker coverage | VERIFIED | 12 tests pass. |
| `tests/unit/test_observability_rates.py` | rolling-window load/save/prune coverage | VERIFIED | 14 tests pass. |
| `tests/unit/test_slack_funnel_blocks.py` | Block Kit JSON snapshot coverage | VERIFIED | 12 tests pass. |
| `tests/unit/test_service_rate_instrumentation.py` | 4-service success/failure semantics | VERIFIED | 18 tests pass. |
| `tests/unit/test_apn_probate_funnel.py` | apn_probate funnel-wiring + Slack-blocks | VERIFIED | 3 tests pass. |
| `tests/unit/test_pre_probate_funnel.py` | pre_probate funnel-wiring + Slack-blocks | VERIFIED | 3 tests pass. |
| `tests/unit/test_main_daily_funnel.py` | main_daily 10-gate funnel + Slack-blocks + W6 negative path | VERIFIED | 4 tests pass (1 over the 3-test spec). |
| `tests/unit/test_benchmark_funnel.py` | benchmark 6-gate funnel + Slack-blocks | VERIFIED | 3 tests pass. |
| `tests/unit/test_tax_distress_funnel.py` | tax_distress 5-gate funnel + Slack-blocks | VERIFIED | 3 tests pass. |
| `tests/unit/test_code_violation_funnel.py` | code_violation 3-gate funnel + Slack-blocks | VERIFIED | 3 tests pass. |

---

## Pipeline Wiring Verification (All 6 Pipelines)

| Pipeline | FunnelCounter | Gates | _send_blocks_webhook | load_rolling | save_rolling | rate_tracker threading |
|---|---|---|---|---|---|---|
| `main.py` (legacy daily) | `FunnelCounter("main_daily", gates=list(MAIN_DAILY_GATES))` | 10 (matches D-01) | 1 call | 1 | 1 | 4 sites — passes rate_tracker into scrape_all, PostScrapeOptions, etc. |
| `apn_probate_pipeline_al.py` | `FunnelCounter("apn_probate", gates=[6])` | 6 (matches D-01) | 1 call | 1 | 1 | 4 sites — passes rate_tracker into batch_skip_trace, property locator. `scrape_all` thread deferred (documented in 02-04 SUMMARY) |
| `pre_probate_pipeline_al.py` | `FunnelCounter("pre_probate", gates=[9])` | 9 (matches D-01) | 1 call | 2 | 1 | 7 sites — LLM, Smarty Madison/Marshall, Tracerfy |
| `benchmark_pipeline_al.py` | `FunnelCounter("benchmark", gates=list(BENCHMARK_GATES))` | 6 (additive — pulled→tier_gated→fiduciary_filtered→obituary_confirmed→tracerfy_matched→datasift_uploaded) | 1 call | 1 | 1 | 5 sites — obituary match LLM, Tracerfy, Smarty |
| `tax_distress_pipeline.py` | `FunnelCounter("tax_distress", gates=TAX_DISTRESS_GATES)` | 5 (matches D-01) | 1 call | 1 | 2 | 2 sites — Smarty Madison/Marshall |
| `code_violation_pipeline.py` | `FunnelCounter("code_violation", gates=CODE_VIOLATION_GATES)` | 3 (matches D-01) | 1 call | 1 | 2 | 0 explicit threads (adapters use internal property API, not instrumented service paths — Slack honestly renders `Smarty: n/a today`) |

**Supporting infrastructure:**

- `src/full_pipeline.py`: 4 `funnel.set` calls (tracerfy_matched + 3 skip-branch zero-fills) + 2 rate_tracker threading sites
- `src/enrichment_pipeline.py`: 6 `funnel.set` calls (county_filtered → zillow_enriched) + 2 rate_tracker threading sites
- `src/scraper.py`: 3 rate_tracker threading sites (scrape_all → run_search → _scrape_notice → solve_captcha_and_view)

All pipelines obey D-03 / W6 ordering:
1. `load_rolling_rates()` BEFORE blocks build (today's post shows prior-days baseline)
2. `_send_blocks_webhook()` POSTs the 3-block payload (D-02 — one message)
3. `save_rolling_rates(rate_tracker.totals())` AFTER success only — guarded by `if sent and rate_tracker is not None`

---

## Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| Every pipeline's `notify_slack` | `slack_notifier._send_blocks_webhook` | builds 3-block payload + POSTs once | WIRED | Confirmed via grep — every pipeline contains exactly one `_send_blocks_webhook` call site |
| Every pipeline `notify_slack` | `observability.save_rolling_rates` | guarded by `if sent and rate_tracker is not None` | WIRED | Confirmed at apn:548, pre:1234, main:106, benchmark:716, tax_distress:413, code_violation:412 |
| Every pipeline `notify_slack` | `observability.load_rolling_rates` | called BEFORE blocks build | WIRED | Confirmed at apn:534, pre:1220, main:96, benchmark:700, tax_distress:402, code_violation:401 |
| 4 services → ServiceRateTracker | per D-04 semantics | record() at each termination path | WIRED | 18 test_service_rate_instrumentation tests pass; per-D-04 semantics for each: 2Captcha (any-attempt clear vs exhausted), Smarty (delivery_line_1 vs empty/HTTPError), Tracerfy (matched/submitted at batch), LLM (parsed AND required_keys present) |
| observability.py | slack_notifier | (must NOT import) | VERIFIED | `grep "slack_notifier\|import slack" src/observability.py` returns 0 — one-way dependency preserved |

---

## CONTEXT.md Decision Compliance (D-01..D-04)

| Decision | Status | Evidence |
|---|---|---|
| D-01: per-pipeline gate sequences (not unified) | VERIFIED | 6 distinct FunnelCounter instances with their own pre-seeded gate sequences — main_daily=10, apn_probate=6, pre_probate=9, benchmark=6, tax_distress=5, code_violation=3. Each pipeline's gate names match the CONTEXT.md D-01 table verbatim. Pre-seeding ensures zero-count gates still render. |
| D-02: one Slack message per run (append funnel to existing post) | VERIFIED | Every pipeline calls `_send_blocks_webhook(text, blocks)` exactly once with a 3-block list (existing summary + funnel + service-rates). `send_slack_notification` is NOT called from the Phase 2 paths. Confirmed via `grep -c "_send_blocks_webhook("` = 1 per pipeline. |
| D-03: today's rate + 7-day rolling baseline | VERIFIED | `load_rolling_rates` called BEFORE blocks build; `save_rolling_rates` called AFTER successful send. `service_rates.json` schema: `{service: [{date, success, total}, ...]}`, pruned to 7 days, atomic tmp+rename write. Tests verify same-date entries are REPLACED, prune-to-7 works across 15-day input. |
| D-04: per-service failure semantics (4 services, exact contract) | VERIFIED | 2Captcha (success = any of 3 attempts cleared gate, failure = exhausted); Smarty (success = delivery_line_1 non-empty, failure = empty or HTTPError); Tracerfy (batch granularity — matched successes + (submitted-matched) failures); LLM (success = parsed JSON AND required_keys present). 18 instrumentation tests verify each semantic at the SDK boundary. |

---

## Wave 1 / Wave 2 Production Code Parity

Verified: existing `send_slack_notification` + `_send_webhook` in `src/slack_notifier.py` are byte-identical to pre-Phase-2. The Slack module is APPEND-ONLY:

- Lines 25-282 preserve legacy send/build/_send_webhook
- Line 286: `send_slack_notification` unchanged
- Lines 333+ are Phase 2 additions (build_funnel_block, build_service_rates_block, _send_blocks_webhook)

`grep -n "def send_slack_notification\|def _send_webhook" src/slack_notifier.py` returns the same 2 functions at original line locations; no rewrites.

Wave 2 service instrumentation is additive: every entry point gets an optional `rate_tracker: ServiceRateTracker | None = None` kwarg. Legacy callers (no kwarg) get byte-identical behavior — 73 baseline Phase 1 tests still pass alongside the 75 new tests added across Wave 1+2+3 (110 total).

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full pytest suite passes | `python -m pytest tests/unit/ -q` | `110 passed, 1 skipped in 3.56s` | PASS |
| Foundation imports work | `python -c "from observability import ...; from slack_notifier import ..."` | All symbols importable | PASS |
| All 6 pipeline names render valid funnel blocks | smoke loop: for each pipeline_name, construct FunnelCounter + build_funnel_block + assert name + counts in text | All 6 pass | PASS |
| TRACKED_SERVICES is correct tuple | `print(TRACKED_SERVICES)` | `('2captcha', 'smarty', 'tracerfy', 'llm')` | PASS |
| STATE_FILE path is correct | `print(STATE_FILE)` | `output/observability/service_rates.json` | PASS |

---

## Requirements Coverage

| Requirement | Description | Status | Evidence |
|---|---|---|---|
| **OPS-03** | Full pipeline funnel transparency on every run — drop counts at every gate (scraped → ZIP-gated → property-matched → obit-matched → tracerfy-matched → uploaded), surfaced in Slack + terminal | SATISFIED | All 6 pipelines emit per-pipeline funnel block in Slack + `logger.info("Funnel (...)")` at end-of-run on terminal. Gate sequences match CONTEXT.md D-01 verbatim. |
| **OBS-01** | Per-service success-rate metric on Slack daily report (2Captcha solve rate, Smarty hit rate, Tracerfy match rate, LLM extraction success rate) — early-warning surfaces before failed runs | SATISFIED (substrate) / PARTIAL (alert) | Per-service rates ARE emitted in Slack with "today \| 7-day" side-by-side. Programmatic yellow/red bands are explicitly deferred to Phase 5+ per CONTEXT.md D-04 — the OBS-01 substrate is delivered; the active alert layer is the explicit deferred scope. |

---

## Phase 1 Regression Check

Phase 1 tests (35 + 1 skip baseline) still pass cleanly alongside the 75 Phase 2 additions. Total: 110 pass + 1 skip. No regressions introduced. Baseline → final test count progression matches summaries exactly:

- Wave 1 (02-01): 35 + 1 → 73 + 1 (+38 tests)
- Wave 2 (02-02): 73 + 1 → 91 + 1 (+18 tests)
- Wave 3 (02-03): 91 + 1 → 97 + 1 (+6 tests)
- Wave 3 (02-04): 97 + 1 → 104 + 1 (+7 tests)
- Wave 3 (02-05): 104 + 1 → 110 + 1 (+6 tests)

---

## Anti-Patterns Found

None blocking. Two informational notes:

| File | Concern | Severity | Impact |
|---|---|---|---|
| `src/apn_probate_pipeline_al.py:177-188` | Inline comment claims `scrape_all` doesn't accept rate_tracker (stale — plan 02-04 added it) | INFO | Apn_probate's pure-CLI invocation still doesn't pass `rate_tracker=` into `scrape_all`, so 2Captcha rate reads `n/a today` for pure-apn_probate runs. Main daily flow IS wired correctly. Documented in 02-04 SUMMARY as a known incremental gap to close in apn_probate's own future revision (not Phase 2 scope). |
| `src/code_violation_pipeline.py` | 0 `rate_tracker=` threading sites | INFO | Adapter internals (Birmingham Accela owner enrich, Hoover SeeClickFix, Huntsville PDF) don't currently call instrumented `address_standardizer.standardize_addresses` paths. Slack honestly renders `Smarty: n/a today` for pure-code_violation runs. Funnel + rolling baseline still functional. |

Both items are intentional and documented as expected behavior in the respective SUMMARYs.

---

## Human Verification Required

None for Phase 2 scope. Phase 2 ships data primitives + Slack rendering + rolling persistence — all observable via the existing pytest suite and module-level smoke checks. No real-time UI behavior, no external service dependency, no visual quality to assess.

**Phase 3 (Unified Daily Scheduler)** will need human verification of the consolidated post format, but that's out of Phase 2 scope.

---

## Deferred Items (Filtered against Phase 3-5 ROADMAP)

| # | Item | Addressed In | Evidence |
|---|---|---|---|
| 1 | Yellow/red alert thresholds (SC-4 active alerting layer) | Phase 5+ | CONTEXT.md D-04 explicit: "Alert thresholds: No alerting in Phase 2. Just emit numbers. Humans decide what's bad." 02-CONTEXT.md `<deferred>` block restates this for Phase 5+. The data substrate IS shipped (per-run rate + 7-day rolling baseline visible side-by-side per service); only the programmatic yellow/red emission is deferred. |
| 2 | apn_probate scrape_all rate_tracker threading (2Captcha rate `n/a today` in pure-CLI runs) | (Known incremental gap — not a phase-blocking gap; main.py daily path already wired) | 02-04 SUMMARY explicitly: "apn_probate's pure-CLI runs will continue reading `n/a today` until that orchestrator instantiates a tracker and passes it into scrape_all itself." The scheduled-daily path (main.py daily mode) DOES pass rate_tracker into scrape_all (scraper.py lines 279/367/602), so the canonical scheduled run that matters is correctly instrumented. |

---

## Gaps Summary

**Phase 2 contract is delivered.** All 3 fully-actionable ROADMAP SCs (SC-1, SC-2, SC-3) are VERIFIED with codebase evidence. SC-4 is PARTIAL by design — the data substrate (per-run rate + 7-day rolling baseline, side-by-side in same Slack block) is shipped, but programmatic yellow/red-band alerting is explicitly deferred to Phase 5+ per CONTEXT.md D-04 and 02-CONTEXT.md `<deferred>`.

Both requirements (OPS-03, OBS-01) are satisfied. All 6 pipelines wired identically per D-01 gate sequences. Wave 1 contract (slack_notifier additive-only) is preserved. 110 tests pass + 1 skip — exact match to executor SUMMARY chain.

**Flags worth carrying into Phase 3:**

1. **apn_probate scrape_all wiring** — Phase 3 unified scheduler will route apn_probate via the consolidated entry point. When that happens, pass the unified `rate_tracker` into apn_probate's `run_pipeline` so 2Captcha rate populates correctly in the rolled-up post.
2. **Service-rate merge logic for Phase 3** — when unifying the 6 per-pipeline trackers, sum `tracker.totals()["service"]` across all 6 first, then derive per-run rate from merged success/total. Don't average the per-pipeline rates (would weight unevenly).
3. **Single `save_rolling_rates` per day** — Phase 3 consolidated post should call `save_rolling_rates(merged_totals)` once after the unified post succeeds, not 6 separate saves.
4. **alert threshold layer (Phase 5+)** — substrate is in place; programmatic alerting (yellow when today vs 7-day diverges by N percentage points) can layer on top of `rolling_rates_summary` + `per_run_rates` without further observability changes.

---

_Verified: 2026-05-24_
_Verifier: Claude (gsd-verifier)_
