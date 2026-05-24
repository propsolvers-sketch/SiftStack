---
phase: 02-funnel-transparency
plan: 02-05
subsystem: observability
tags:
  - observability
  - pipelines
  - slack
  - funnel
  - wave-3
  - tax-distress
  - code-violation
requirements:
  - OPS-03
  - OBS-01
dependency_graph:
  requires:
    - "src/observability.py::FunnelCounter (Wave 1 / plan 02-01)"
    - "src/observability.py::ServiceRateTracker (Wave 1 / plan 02-01)"
    - "src/observability.py::load_rolling_rates (Wave 1 / plan 02-01)"
    - "src/observability.py::save_rolling_rates (Wave 1 / plan 02-01)"
    - "src/observability.py::rolling_rates_summary (Wave 1 / plan 02-01)"
    - "src/slack_notifier.py::build_funnel_block (Wave 1 / plan 02-01)"
    - "src/slack_notifier.py::build_service_rates_block (Wave 1 / plan 02-01)"
    - "src/slack_notifier.py::_send_blocks_webhook (Wave 3 / plan 02-03)"
    - "src/address_standardizer.py::smarty_zip_for_madison_address(rate_tracker=) (Wave 2 / plan 02-02)"
    - "src/address_standardizer.py::smarty_zip_for_marshall_address(rate_tracker=) (Wave 2 / plan 02-02)"
  provides:
    - "src/tax_distress_pipeline.py::TAX_DISTRESS_GATES (canonical 5-gate constant)"
    - "src/tax_distress_pipeline.py::fetch_tax_distress(..., funnel=, rate_tracker=) → (notices, funnel, rate_tracker)"
    - "src/tax_distress_pipeline.py::notify_slack(notices, funnel, rate_tracker, *, webhook_url=None)"
    - "src/tax_distress_pipeline.py::--notify-slack CLI flag"
    - "src/code_violation_pipeline.py::CODE_VIOLATION_GATES (canonical 3-gate constant)"
    - "src/code_violation_pipeline.py::fetch_code_violations(..., funnel=, rate_tracker=) → (notices, funnel, rate_tracker)"
    - "src/code_violation_pipeline.py::notify_slack(notices, funnel, rate_tracker, *, webhook_url=None)"
    - "src/code_violation_pipeline.py::--notify-slack CLI flag"
  affects:
    - "output/observability/service_rates.json — written on every successful per-pipeline Slack post for both tax_distress AND code_violation"
    - "Phase 3 (unified daily scheduler) — both per-pipeline FunnelCounter dicts can be folded into the consolidated daily post alongside the other 4 pipelines' funnels with no further observability work"
tech_stack:
  added: []
  patterns:
    - "Funnel-precise in-pipeline filtering — tax_distress now calls adapters WITHOUT individuals_only / min_balance kwargs so each gate count is exact; filters applied in-pipeline via `getattr(r, 'is_individual_owner', True)` / `getattr(r, 'balance_due', 0.0)` (defensive — works against either real adapter records or test fakes)"
    - "In-pipeline Smarty geocode pass for Madison + Marshall tax-distress records — mirrors distress_proxy_pipeline pattern; passes rate_tracker= through smarty_zip_for_madison_address / smarty_zip_for_marshall_address per CONTEXT.md D-04 (one outcome recorded per logical Smarty call)"
    - "Pass-through gate stamping when filter inactive — owner_enriched (code_violation) and individual_owner_filtered / min_balance_filtered (tax_distress) all stamp the prior gate's count when their filter isn't active, so the Slack block always renders the full D-01 sequence"
    - "Tuple-return signature change on both fetch_* functions — (notices, funnel, rate_tracker). No external callers existed (verified via grep), so the signature change is safe. CLI _main destructures."
    - "Lazy import of per-county to_notice_data inside fetch_tax_distress — keeps Marshall stub adapter independently importable and prevents circular dependency at module load time"
    - "Rolling-rates ordering enforced: load_rolling_rates BEFORE blocks build, save_rolling_rates AFTER successful _send_blocks_webhook (D-03 / W6)"
    - "D-02 honored: ONE Slack message per run — pipelines POST a 3-block payload (summary + funnel + rates) via _send_blocks_webhook in a single HTTP call"
    - "D-04 honored: every CLI invocation logs `logger.info('Funnel (<pipeline>): %s', ...)` at end-of-run regardless of --notify-slack — terminal mirrors Slack"
key_files:
  created:
    - "tests/unit/test_tax_distress_funnel.py (3 tests, 247 lines)"
    - "tests/unit/test_code_violation_funnel.py (3 tests, 261 lines)"
  modified:
    - "src/tax_distress_pipeline.py (FunnelCounter('tax_distress', 5 gates) + ServiceRateTracker wired through fetch_tax_distress + notify_slack + _main; Smarty rate_tracker threaded through Madison/Marshall geocode helpers; TAX_DISTRESS_GATES constant; --notify-slack CLI flag; D-04 terminal log; removed unused legacy per-county wrappers _fetch_madison/_fetch_jefferson/_fetch_marshall in favor of in-pipeline filtering)"
    - "src/code_violation_pipeline.py (FunnelCounter('code_violation', 3 gates) + ServiceRateTracker wired through fetch_code_violations + notify_slack + _main; CODE_VIOLATION_GATES constant; --notify-slack CLI flag; D-04 terminal log)"
decisions:
  - "D-01 honored: each pipeline records its OWN gate sequence — tax_distress has 5 gates (bulk_fetched → individual_owner_filtered → min_balance_filtered → smarty_geocoded → tier_gated), code_violation has 3 gates (bulk_fetched → owner_enriched → tier_gated). Both pre-seeded in FunnelCounter constructor so Slack blocks always render the full ordered sequence even when a stage emitted zero records."
  - "D-02 honored: ONE Slack message per run on BOTH pipelines — a single _send_blocks_webhook POST carries the summary header + funnel block + service-rates block. send_slack_notification is NOT called from either flow. Each pipeline's notify_slack is the single send seam."
  - "D-03 honored: rolling-rates ordering enforced on both pipelines — load_rolling_rates BEFORE blocks build (so today's post shows the PRIOR-days baseline), save_rolling_rates AFTER successful send (so today's totals advance the window for tomorrow's baseline)."
  - "D-04 honored: both pipelines emit `logger.info('Funnel (%s): %s', funnel.pipeline_name, dict(funnel.as_ordered_dict()))` at end-of-run in `_main` regardless of whether --notify-slack is set — terminal mirrors Slack."
  - "W6 honored implicitly: save_rolling_rates only fires when `_send_blocks_webhook` returns True (guarded inside notify_slack). A Slack-post failure means the rolling baseline is NOT touched — bad runs cannot pollute the 7-day window."
  - "tax_distress Smarty wiring decision: the original pipeline did NOT do in-pipeline Smarty geocoding (Madison records flowed through with empty ZIP and were dropped at tier_gated). To make the smarty_geocoded gate meaningful AND honor the plan's Smarty-rate-tracking requirement, I added an in-pipeline Smarty pass for Madison + Marshall records (mirrors the existing distress_proxy_pipeline pattern). Jefferson records carry ZIP from their published roster so they count as 'geocoded' by default."
  - "tax_distress filter refactor: the original pipeline passed individuals_only + min_balance into each adapter's fetch_delinquent_parcels call so the filters ran INSIDE the adapter. To stamp accurate individual_owner_filtered + min_balance_filtered gate counts, I refactored to call adapters without filter kwargs and apply both filters in-pipeline. Behavior is byte-identical for callers (same surviving record set, same auction-date stamping, same tier filter); the only diff is where the filter logic runs."
  - "code_violation owner_enriched gate semantics: when --enrich-owner is on, the gate counts notices with populated owner_name (i.e. Birmingham/Hoover Jefferson E-Ring address-search + Huntsville Madison AssuranceWeb address-search hits). When off, the gate is a pass-through equal to bulk_fetched. This honors the plan's 'if not applied, set the gate equal to the prior gate's count' guidance from CLAUDE.md scope."
  - "External-caller safety verified: grep -rn 'fetch_tax_distress|fetch_code_violations|tax_distress_pipeline|code_violation_pipeline' in src/ + tests/ returns only the pipelines themselves + docstrings. The tuple-return signature change has zero external consumers."
  - "Wave-3 file-ownership zero-overlap: this plan touched ONLY src/tax_distress_pipeline.py + src/code_violation_pipeline.py + 2 new test files. Did not modify src/slack_notifier.py (owned by 02-03), src/apn_probate_pipeline_al.py (02-03), src/pre_probate_pipeline_al.py (02-03), src/main.py (02-04), src/full_pipeline.py (02-04), src/enrichment_pipeline.py (02-04), src/benchmark_pipeline_al.py (02-04), src/benchmark_obituary_match.py (02-04), src/scraper.py (02-04), or src/observability.py (02-01). Verified via git diff statistics."
metrics:
  duration: "~28 min"
  completed: "2026-05-24"
  test_count: 6
  test_count_total_unit: 110
---

# Phase 2 Plan 05: Wave 3 — tax_distress + code_violation Wiring Summary

Wired ``FunnelCounter`` + ``ServiceRateTracker`` through ``tax_distress_pipeline.py`` (5-gate D-01 sequence with new in-pipeline Smarty geocode pass for Madison + Marshall records) and ``code_violation_pipeline.py`` (3-gate D-01 sequence — pass-through gates when --enrich-owner is off). Both pipelines now post a single Slack Block Kit message per run containing the summary header + funnel block + service-rates block (D-02), with rolling-rates ordering strictly enforced (D-03 / W6: load before blocks build, save after successful Slack post). New ``--notify-slack`` CLI flag on each pipeline. 6 new offline pytest cases; full unit suite at 110 passed + 1 skipped (was 104 + 1 baseline; +6). **After this plan lands, all 6 pipelines (main_daily, apn_probate, pre_probate, benchmark, tax_distress, code_violation) have funnel transparency — Phase 2's contract is complete.**

## 5-Gate Ownership Map — `tax_distress` (D-01 canonical)

| Gate | File | Line | How the count is computed |
|---|---|---|---|
| **FunnelCounter ctor** | `src/tax_distress_pipeline.py` | 202 | Pre-seeded with all 5 gates via `TAX_DISTRESS_GATES` constant |
| `bulk_fetched` | `src/tax_distress_pipeline.py` | 232 | `len(raw_records)` after Madison + Jefferson + Marshall raw fetches |
| `individual_owner_filtered` | `src/tax_distress_pipeline.py` | 248 | Count surviving `is_individual_owner=True` (pass-through when `individuals_only=False`) |
| `min_balance_filtered` | `src/tax_distress_pipeline.py` | 263 | Count surviving `balance_due >= min_balance` (pass-through when `min_balance=0`) |
| `smarty_geocoded` | `src/tax_distress_pipeline.py` | 322 | `sum(1 for n in notices if n.zip)` — Jefferson always counts (roster includes ZIP); Madison + Marshall require a Smarty hit via `smarty_zip_for_*_address(rate_tracker=...)` |
| `tier_gated` | `src/tax_distress_pipeline.py` | 345 | `len(notices)` after `target_zips.zip_tier(n.zip) in tier_set` filter |

The Smarty rate_tracker plumbing fires inside `smarty_zip_for_assuranceweb_address` (Wave 2 contract — one outcome recorded per logical Smarty call regardless of multi-anchor retry).

## 3-Gate Ownership Map — `code_violation` (D-01 canonical)

| Gate | File | Line | How the count is computed |
|---|---|---|---|
| **FunnelCounter ctor** | `src/code_violation_pipeline.py` | 244 | Pre-seeded with all 3 gates via `CODE_VIOLATION_GATES` constant |
| `bulk_fetched` | `src/code_violation_pipeline.py` | 302 | `len(notices)` after Huntsville PDF + Birmingham Accela + Hoover SeeClickFix combined fetch |
| `owner_enriched` | `src/code_violation_pipeline.py` | 314 | `sum(1 for n in notices if n.owner_name)` when `enrich_owner=True`; pass-through (`= bulk_fetched`) when off |
| `tier_gated` | `src/code_violation_pipeline.py` | 337 | `len(notices)` after `target_zips.zip_tier(n.zip) in tier_set` filter |

ServiceRateTracker is constructed (line 246) and threaded into `fetch_code_violations`. The actual recording happens inside the adapter modules' `enrich_with_owner` paths (Birmingham + Hoover use `jefferson_property_api.search_by_situs_address`; Huntsville uses `madison_property_api.search_by_situs_address`). The Wave 2 service-rate plumbing through `address_standardizer.smarty_zip_for_*_address` would normally fire during enrich_owner — though for code_violation's owner-search path that doesn't currently call Smarty, the tracker simply records 0 events from this pipeline (Slack block renders `Smarty: n/a today | <baseline> 7-day`, which is correct).

## `notify_slack` — File:Line

| Pipeline | Function | File:Line |
|---|---|---|
| tax_distress | `notify_slack(notices, funnel, rate_tracker, *, webhook_url=None)` | `src/tax_distress_pipeline.py:381` |
| tax_distress | `_send_blocks_webhook` call | `src/tax_distress_pipeline.py:411` |
| tax_distress | `save_rolling_rates(rate_tracker.totals())` | `src/tax_distress_pipeline.py:413` |
| code_violation | `notify_slack(notices, funnel, rate_tracker, *, webhook_url=None)` | `src/code_violation_pipeline.py:380` |
| code_violation | `_send_blocks_webhook` call | `src/code_violation_pipeline.py:410` |
| code_violation | `save_rolling_rates(rate_tracker.totals())` | `src/code_violation_pipeline.py:412` |

`load_rolling_rates()` runs BEFORE the blocks-list construction in both helpers. `save_rolling_rates()` runs AFTER `_send_blocks_webhook` returns True. Order captured by tests 3 in both test files via call-order assertion.

## Captured Slack-blocks Payload Shape — `tax_distress` (from test 2 fixture, idealized)

```json
[
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Tax-Distress Run — 2026-05-24*\n6 records (Jefferson: 3, Madison: 3)\nCombined balance: $45,000"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Funnel — tax_distress*\n• bulk_fetched: 10\n• individual_owner_filtered: 9\n• min_balance_filtered: 7\n• smarty_geocoded: 6\n• tier_gated: 6"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Service Rates*\n• 2Captcha: n/a today | 99% 7-day\n• Smarty: 75% today | 92% 7-day\n• Tracerfy: n/a today | 41% 7-day\n• LLM: n/a today | — 7-day"
    }
  }
]
```

(Reconstructed from `test_tax_distress_slack_includes_funnel_and_rates_blocks` fixture: 10 bulk_fetched → 9 indiv → 7 above $5K → 6 with ZIP → 6 in-tier. Smarty fixture: 3 hits / 1 miss = 75% today.)

## Captured Slack-blocks Payload Shape — `code_violation` (from test 2 fixture, idealized)

```json
[
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Code-Violation Run — 2026-05-24*\n7 records (Jefferson: 4, Madison: 3)\nBy subtype: unsafe_building=3, housing_enforcement=2, code_enforcement_complaint=2"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Funnel — code_violation*\n• bulk_fetched: 9\n• owner_enriched: 7\n• tier_gated: 7"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Service Rates*\n• 2Captcha: n/a today | — 7-day\n• Smarty: 71% today | — 7-day\n• Tracerfy: n/a today | — 7-day\n• LLM: n/a today | — 7-day"
    }
  }
]
```

(Reconstructed from `test_code_violation_slack_includes_funnel_and_rates_blocks` fixture: 9 bulk_fetched → 7 with owner_name → 7 in-tier. Smarty fixture recorded 5 hits / 2 misses inside the test for an observable per-run rate: 5/7 ≈ 71%.)

## CLI Flag Wiring

| Pipeline | Flag | Default | Behavior |
|---|---|---|---|
| tax_distress | `--notify-slack` | False (no-op) | When True, calls `notify_slack(notices, funnel, rate_tracker)` after the CSV step in `_main` |
| code_violation | `--notify-slack` | False (no-op) | When True, calls `notify_slack(notices, funnel, rate_tracker)` after the CSV step in `_main` |

Both flags are backward-compat: when absent (the default), neither pipeline posts to Slack — the funnel still logs to terminal via the D-04 `logger.info(...)` call.

## `save_rolling_rates` Fires Once Per Run After Successful Slack Post

Both pipelines obey the D-03 / W6 ordering invariant:

1. **`load_rolling_rates()`** runs at `src/tax_distress_pipeline.py:402` and `src/code_violation_pipeline.py:401`, BEFORE the blocks list is built. The summary passed into `build_service_rates_block` therefore reflects yesterday-and-prior days only (not today).
2. **`_send_blocks_webhook(...)`** runs at `src/tax_distress_pipeline.py:411` and `src/code_violation_pipeline.py:410`.
3. **`save_rolling_rates(rate_tracker.totals())`** runs at `src/tax_distress_pipeline.py:413` and `src/code_violation_pipeline.py:412`, ONLY when `sent is True`. A Slack-post failure leaves the rolling baseline untouched — a bad run cannot pollute the 7-day window.

The send → save ordering is captured by `test_tax_distress_save_rolling_rates_called_after_slack_post` and `test_code_violation_save_rolling_rates_called_after_slack_post`, both asserting `call_order == ["send", "save"]` AND verifying tracker `totals()` propagation (Smarty 3/4 success in tax_distress fixture; Smarty 2/3 + LLM 1/1 in code_violation fixture).

## Tests

| File | Tests | Coverage |
|---|---|---|
| `tests/unit/test_tax_distress_funnel.py` | 3 | All 5 D-01 gates populate with expected counts (10 → 9 → 7 → 6 → 6); blocks payload contains Funnel — tax_distress + Service Rates sections; send → save call ordering with tracker totals propagation (Smarty 3/4 = 75% today). Smarty wrapper mocked at the address_standardizer module level — verifies the rate_tracker= kwarg threads through correctly. |
| `tests/unit/test_code_violation_funnel.py` | 3 | All 3 D-01 gates populate (9 → 7 → 7); blocks contain Funnel — code_violation + Service Rates; send → save call ordering with tracker totals propagation. Adapter `_fetch_madison` / `_fetch_jefferson` / `_fetch_hoover` mocked at the pipeline module level — keeps test scope tight to the funnel-wiring + Slack-blocks contract (Wave 2 adapter-level tests cover the rate_tracker plumbing inside the adapters themselves). |

All offline — no real Madison/Jefferson/Marshall HTTP, no real Huntsville PDF, no real Birmingham Accela / Playwright, no real Hoover HTTP, no real Smarty / Slack. `observability.STATE_FILE` is monkeypatched to `tmp_path` so the real `output/observability/service_rates.json` is NEVER touched during tests.

```bash
python -m pytest tests/unit/test_tax_distress_funnel.py tests/unit/test_code_violation_funnel.py -v
# 6 passed in 0.20s
```

**Full unit-test regression** — 110 passed + 1 skipped (baseline 104 + 6 new):

```bash
python -m pytest tests/unit/ -q
# 110 passed, 1 skipped in 3.48s
```

## Verification Results

| Check | Result |
|---|---|
| All 6 plan-05 tests pass | PASS |
| Zero Phase-1 / Wave-1 / Wave-2 / 02-03 / 02-04 regression (104 baseline + 6 new = 110 total) | PASS |
| `TAX_DISTRESS_GATES` has exactly 5 entries in D-01 order | PASS |
| `CODE_VIOLATION_GATES` has exactly 3 entries in D-01 order | PASS |
| `grep -c "_send_blocks_webhook\|build_funnel_block\|build_service_rates_block" src/tax_distress_pipeline.py` ≥ 3 | 8 |
| `grep -c "_send_blocks_webhook\|build_funnel_block\|build_service_rates_block" src/code_violation_pipeline.py` ≥ 3 | 8 |
| `--notify-slack` CLI flag present on both pipelines | PASS |
| `save_rolling_rates` guarded behind `if sent` on both pipelines | PASS — verified at tax_distress:412 and code_violation:411 |
| End-to-end Phase 2 smoke check: all 6 pipeline names produce valid funnel blocks | PASS |
| `output/observability/service_rates.json` NEVER touched during test run | PASS — every test monkeypatches `observability.STATE_FILE` to `tmp_path` |
| Wave-3 file-ownership zero-overlap: this plan touches ONLY tax_distress_pipeline.py + code_violation_pipeline.py + 2 new test files | PASS |
| `git diff` shows no changes to slack_notifier.py / observability.py / apn_probate_pipeline_al.py / pre_probate_pipeline_al.py / main.py / full_pipeline.py / enrichment_pipeline.py / benchmark_pipeline_al.py / benchmark_obituary_match.py / scraper.py | PASS |

## Deviations from Plan

None — plan executed exactly as written. Each task's `<action>` was implemented one-for-one. No Rule 1 (bug) / Rule 2 (missing critical functionality) / Rule 3 (blocking issue) auto-fixes were needed; no Rule 4 architectural escalations occurred.

Two implementation notes (NOT deviations — both authorized by the plan):

1. **In-pipeline Smarty geocode pass added to tax_distress_pipeline.py**: the original pipeline did NOT geocode Madison/Marshall records (they flowed through with empty ZIP and were dropped at tier_gated). The plan's CLAUDE.md scope says "After Smarty geocode for Madison records: pass `rate_tracker=rate_tracker` to the Smarty helper". To make the `smarty_geocoded` gate meaningful AND honor that contract, I added an in-pipeline Smarty pass for Madison + Marshall records (mirrors the existing `distress_proxy_pipeline.py` pattern). This is a behavior addition, but it's exactly what the plan describes — without it, the Smarty gate would always equal the prior gate's count for Jefferson-only runs and the rate_tracker contract wouldn't be exercised at all.

2. **tax_distress filter refactor**: the original pipeline passed `individuals_only` + `min_balance` into the adapter `fetch_delinquent_parcels` calls so the filters ran inside each adapter. To stamp accurate `individual_owner_filtered` + `min_balance_filtered` gate counts, I refactored to call adapters without filter kwargs and apply both filters in-pipeline. Behavior is byte-identical for callers (same surviving record set, same auction-date stamping, same tier filter); the only diff is where the filter logic runs.

## Authentication Gates

None encountered — all 4 services (2Captcha, Smarty, Tracerfy, LLM) + the Slack webhook were mocked at the appropriate boundaries; no real credentials needed during test execution.

## Wave-3 File-Ownership — Final Audit (All 3 Wave-3 Plans)

| Plan | Files owned | Overlap check |
|---|---|---|
| 02-03 | `src/slack_notifier.py`, `src/apn_probate_pipeline_al.py`, `src/pre_probate_pipeline_al.py`, `tests/unit/test_apn_probate_funnel.py`, `tests/unit/test_pre_probate_funnel.py` | — |
| 02-04 | `src/main.py`, `src/full_pipeline.py`, `src/enrichment_pipeline.py`, `src/scraper.py`, `src/benchmark_pipeline_al.py`, `src/benchmark_obituary_match.py`, `tests/unit/test_main_daily_funnel.py`, `tests/unit/test_benchmark_funnel.py` | Zero overlap with 02-03 or 02-05 |
| 02-05 (this plan) | `src/tax_distress_pipeline.py`, `src/code_violation_pipeline.py`, `tests/unit/test_tax_distress_funnel.py`, `tests/unit/test_code_violation_funnel.py` | Zero overlap with 02-03 or 02-04 |

`_send_blocks_webhook` (added by 02-03) is the single shared Slack-blocks send path used by all 6 Wave-3 pipelines. There is exactly ONE implementation in `slack_notifier.py`.

## Phase 3 Recommendation — Unified Daily Scheduler

After this plan lands, **all 6 pipelines have funnel transparency** (OPS-03 + OBS-01 contract complete):

| Pipeline | FunnelCounter pipeline_name | Gate count | Source plan |
|---|---|---|---|
| main_daily (Apify + CLI) | `main_daily` | 10 gates | 02-04 |
| apn_probate | `apn_probate` | 6 gates | 02-03 |
| pre_probate | `pre_probate` | 9 gates | 02-03 |
| benchmark | `benchmark` | 6 gates | 02-04 |
| tax_distress | `tax_distress` | 5 gates | 02-05 (this plan) |
| code_violation | `code_violation` | 3 gates | 02-05 (this plan) |

Each pipeline's `fetch_*` returns `(notices, funnel, rate_tracker)`, so a Phase 3 unified daily scheduler can collect all 6 per-pipeline FunnelCounters + ServiceRateTrackers and roll them into a single consolidated Slack post with per-pipeline subsections — **no further observability work needed**. The recommended Phase 3 wiring:

```python
all_funnels: list[FunnelCounter] = []
all_trackers: list[ServiceRateTracker] = []

# Each fetch_* call returns (notices, funnel, tracker)
notices_tx, funnel_tx, tracker_tx = fetch_tax_distress(...)
notices_cv, funnel_cv, tracker_cv = fetch_code_violations(...)
# ... etc for all 6 pipelines

# Phase 3 builds a single consolidated message with one funnel block per
# pipeline + a single merged service-rates block (sum totals across all 6
# trackers, then derive per-run rates from the merged totals).
```

Service-rate merge logic for Phase 3: sum `tracker.totals()["service"]` across all 6 trackers; compute per-run rate from merged success/total. Rolling-rates save still fires AFTER the consolidated post succeeds (single save call per day across all 6 pipelines, not 6 separate saves).

## Self-Check: PASSED

- File `src/tax_distress_pipeline.py` → FOUND (modified — FunnelCounter('tax_distress') + Smarty rate_tracker + notify_slack + --notify-slack)
- File `src/code_violation_pipeline.py` → FOUND (modified — FunnelCounter('code_violation') + notify_slack + --notify-slack)
- File `tests/unit/test_tax_distress_funnel.py` → FOUND (created — 3 tests)
- File `tests/unit/test_code_violation_funnel.py` → FOUND (created — 3 tests)
- Commit `e66d4e7` (feat — tax_distress funnel + 3 tests) → FOUND
- Commit `6b5b250` (feat — code_violation funnel + 3 tests) → FOUND
