---
phase: 02-funnel-transparency
plan: 02-04
subsystem: observability
tags:
  - observability
  - pipelines
  - slack
  - funnel
  - wave-3
  - main-daily
  - benchmark
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
    - "src/slack_notifier.py::build_summary (existing; reused as-is)"
    - "src/captcha_solver.py::solve_captcha_and_view(rate_tracker=) (Wave 2 / plan 02-02)"
    - "src/address_standardizer.py::standardize_addresses(rate_tracker=) (Wave 2 / plan 02-02)"
    - "src/llm_client.py::chat_json(rate_tracker=, required_keys=) (Wave 2 / plan 02-02)"
    - "src/tracerfy_skip_tracer.py::batch_skip_trace(rate_tracker=) (Wave 2 / plan 02-02)"
  provides:
    - "src/full_pipeline.py::PostScrapeOptions(funnel=, rate_tracker=)"
    - "src/enrichment_pipeline.py::PipelineOptions(funnel=, rate_tracker=)"
    - "src/main.py::MAIN_DAILY_GATES (canonical 10-gate constant)"
    - "src/main.py::_post_daily_slack_with_funnel (testable Slack seam)"
    - "src/scraper.py::scrape_all(rate_tracker=)"
    - "src/scraper.py::run_search(rate_tracker=)"
    - "src/scraper.py::_scrape_notice(rate_tracker=)"
    - "src/benchmark_pipeline_al.py::BENCHMARK_GATES (canonical 6-gate constant)"
    - "src/benchmark_pipeline_al.py::run_pipeline(funnel=, rate_tracker=) → (results, funnel, rate_tracker)"
    - "src/benchmark_pipeline_al.py::prepare_notices(funnel=, rate_tracker=)"
    - "src/benchmark_pipeline_al.py::notify_slack(funnel=, rate_tracker=)"
    - "src/benchmark_obituary_match.py::_parse_obituary_for_petitioner(rate_tracker=)"
    - "src/benchmark_obituary_match.py::match_petitioner_city(rate_tracker=)"
    - "src/benchmark_obituary_match.py::_OBIT_MATCH_REQUIRED_KEYS"
  affects:
    - "Phase 3 (unified daily scheduler) — main_daily funnel + benchmark funnel are now both consumable by the future rollup post"
    - "output/observability/service_rates.json — written on every successful per-pipeline Slack post for both main_daily AND benchmark"
tech_stack:
  added: []
  patterns:
    - "Options-container threading (PostScrapeOptions / PipelineOptions get funnel + rate_tracker fields with default None — legacy CSV/PDF/photo callers byte-identical)"
    - "MAIN_DAILY_GATES / BENCHMARK_GATES module-constant tuples pin canonical gate order so both entry paths (Apify Actor + CLI) instantiate identically"
    - "Single testable Slack seam (_post_daily_slack_with_funnel) so test_main_daily_funnel tests the gate-stamping + block-payload logic without driving the Apify Actor or CLI argparse"
    - "scraper.py additive rate_tracker kwarg through scrape_all → run_search → _scrape_notice → solve_captcha_and_view — resolves the 2Captcha-rate deferral noted in 02-03"
    - "Skip-branch zero-fill — every gate stamps unconditionally on every code path (skip_tracerfy / no credentials / exception / empty input) so the Slack block always renders the full sequence per D-01 invariant"
    - "Negative-path test coverage — Test 4 (test_main_daily_save_skipped_when_slack_post_fails) explicitly verifies W6 (failed send leaves rolling baseline untouched)"
    - "WARNING 7 resolution — benchmark_obituary_match._OBIT_MATCH_REQUIRED_KEYS module constant pins the routing keys ('is_decedent_match', 'petitioner_match') the caller uses to grade confidence; missing either makes the response malformed and counts as an LLM failure in the rate tracker"
key_files:
  created:
    - "tests/unit/test_main_daily_funnel.py (4 tests — 1 over the 3-test spec)"
    - "tests/unit/test_benchmark_funnel.py (3 tests)"
  modified:
    - "src/full_pipeline.py — PostScrapeOptions(funnel=, rate_tracker=) added; run_full_pipeline forwards both into PipelineOptions, stamps tracerfy_matched gate (+ 3 skip-branch zero-fills), passes rate_tracker into batch_skip_trace"
    - "src/enrichment_pipeline.py — PipelineOptions(funnel=, rate_tracker=) added; run_enrichment_pipeline stamps 6 enrichment-stage gates (county_filtered → zillow_enriched) — every gate stamps unconditionally so they always appear; threads rate_tracker into standardize_addresses + retry_with_geocoded_city"
    - "src/scraper.py — scrape_all + run_search + _scrape_notice all accept rate_tracker; threads through solve_captcha_and_view (resolves the 02-03 2Captcha-rate deferral)"
    - "src/main.py — instantiates FunnelCounter('main_daily', 10 gates) + ServiceRateTracker in BOTH actor_main (Apify path) AND _run_scrape_pipeline (CLI path); owns scraped + seen_ids_deduped + datasift_uploaded gates locally; threads funnel + rate_tracker via PostScrapeOptions into full_pipeline; _post_daily_slack_with_funnel helper builds 3-block payload (legacy summary + funnel block + service-rates block) and POSTs via _send_blocks_webhook in one HTTP call; rolling-rates ordering (D-03) + save-on-success-only (W6) enforced inside the helper; end-of-run funnel logger.info on both paths (D-04); MAIN_DAILY_GATES module constant"
    - "src/benchmark_pipeline_al.py — FunnelCounter('benchmark', 6 gates) + ServiceRateTracker wired through run_pipeline (now returns 3-tuple) + prepare_notices + notify_slack + _cli; BENCHMARK_GATES module constant; notify_slack bifurcates legacy text path vs Phase 2 blocks path (W5 preserved); rolling-rates ordering (D-03) + save-on-success-only (W6) enforced; D-04 terminal log in _cli; threads rate_tracker into match_petitioner_city + batch_skip_trace"
    - "src/benchmark_obituary_match.py — _OBIT_MATCH_REQUIRED_KEYS = ('is_decedent_match', 'petitioner_match'); _parse_obituary_for_petitioner accepts rate_tracker, passes both kwargs into llm_client.chat_json (WARNING 7 resolution); threaded through match_petitioner_city + enrich_via_ancestry + batch_ancestry_fallback so any caller path that supplies a tracker gets the recording"
decisions:
  - "D-01 honored: every gate in both pipelines is pre-seeded via FunnelCounter constructor AND stamped unconditionally on every code path (success / skip / exception / empty input) so the Slack block always renders the full sequence"
  - "D-02 honored: ONE Slack message per run on BOTH pipelines — a single _send_blocks_webhook POST carries the legacy summary section + funnel block + service-rates block. send_slack_notification is no longer called from the daily flows (W5 preserved — it stays byte-identical in slack_notifier.py for any caller that still uses it directly)"
  - "D-03 honored: rolling-rates ordering enforced on both pipelines — load_rolling_rates BEFORE blocks build (so today's post shows the PRIOR-days baseline), save_rolling_rates AFTER successful send (so today's totals advance the window for tomorrow's baseline). Failed sends leave the baseline untouched (W6)"
  - "D-04 honored: both pipelines emit `logger.info('Funnel (%s): %s', funnel.pipeline_name, dict(funnel.as_ordered_dict()))` at end-of-run regardless of whether --notify-slack is set — terminal mirrors Slack"
  - "W5 resolution: the daily flows call _send_blocks_webhook DIRECTLY (via main._post_daily_slack_with_funnel and benchmark notify_slack's Phase 2 branch). send_slack_notification was NOT modified; the legacy text-only path through _send_webhook stays byte-identical (plan 02-01's additive-only contract preserved)"
  - "W6 resolution: save_rolling_rates is guarded behind `if sent` on both pipelines. A Slack-post failure means the rolling baseline is NOT touched — bad runs cannot pollute the 7-day window. Test 4 (test_main_daily_save_skipped_when_slack_post_fails) explicitly verifies this negative path"
  - "W7 resolution: benchmark_obituary_match._parse_obituary_for_petitioner passes required_keys=('is_decedent_match', 'petitioner_match') to llm_client.chat_json. These are the routing keys the caller uses to grade confidence (petitioner_match ∈ {exact, fuzzy, not_found}; is_decedent_match ∈ {true, false}). A response missing either key is malformed and counts as an LLM failure in the rate tracker"
  - "W8 honored: benchmark is documented in must_haves as additive scope beyond CONTEXT.md D-01's 5-pipeline list. CONTEXT.md D-01 stays as-written (additive, not contradictory). The BENCHMARK_GATES constant pins the 6-gate sequence (pulled → tier_gated → fiduciary_filtered → obituary_confirmed → tracerfy_matched → datasift_uploaded) so the addition is auditable"
  - "Plan-checker observation #2 resolved: scraper.scrape_all now accepts rate_tracker (additive kwarg, default=None — same pattern as 02-02's service entry points). Threaded through scrape_all → run_search → _scrape_notice → solve_captcha_and_view. After this lands, the legacy daily pipeline's 2Captcha rate stops reading 'n/a today'. apn_probate's pure-CLI runs will continue reading 'n/a today' until they instantiate a tracker and pass it into scrape_all themselves — which is the apn_probate orchestrator's responsibility, NOT this plan's scope"
  - "10-gate ownership map executed exactly per the plan table: main.py owns scraped + seen_ids_deduped + datasift_uploaded; enrichment_pipeline.py owns county_filtered + parsed + tier_gated + al_property_enriched + smarty_standardized + zillow_enriched; full_pipeline.py owns tracerfy_matched. Same FunnelCounter instance threads through all 3 files via PostScrapeOptions → PipelineOptions"
  - "Test scope decision: test_main_daily_funnel tests `_post_daily_slack_with_funnel` directly (a small testable seam in main.py) rather than driving the full Apify Actor or _run_scrape_pipeline through argparse. This keeps the test surface small while still exercising the full funnel + slack-blocks path. The synthetic _drive_synthetic_daily_funnel helper stamps the same 10 gates main.py + full_pipeline + enrichment_pipeline would stamp in a real run, in the same order"
  - "Files_modified frontmatter expansion: src/scraper.py was added to this plan's files_modified set because the plan-checker observation #2 + 02-03 SUMMARY explicitly flagged scrape_all rate_tracker threading as 02-04's responsibility. Wave-3 ownership audit confirms zero overlap with 02-03 (slack_notifier.py / apn_probate_pipeline_al.py / pre_probate_pipeline_al.py) or 02-05 (tax_distress_pipeline.py / code_violation_pipeline.py)"
metrics:
  duration: "~45 min"
  completed: "2026-05-24"
  test_count: 7
  test_count_total_unit: 104
---

# Phase 2 Plan 04: Wave 3 — main.py daily + benchmark Wiring Summary

Wired ``FunnelCounter`` + ``ServiceRateTracker`` through the legacy ``main.py daily`` pipeline (10-gate D-01 sequence distributed across main.py + full_pipeline.py + enrichment_pipeline.py — single FunnelCounter instance accumulates across all 3 files via PostScrapeOptions → PipelineOptions threading) AND ``benchmark_pipeline_al.py`` (6-gate sequence — additive scope per WARNING 8). Added ``rate_tracker`` kwarg to ``scraper.scrape_all`` (resolves the 02-03 2Captcha-rate deferral). Wired ``required_keys=("is_decedent_match", "petitioner_match")`` into the benchmark obituary-match LLM call site (WARNING 7). Both pipelines now post a single Slack Block Kit message per run containing the existing summary text + funnel block + service-rates block (D-02), with rolling-rates ordering strictly enforced (D-03 / W6: load before blocks build, save after successful Slack post, skip save on send failure). 7 new offline pytest cases; full unit suite at 104 passed + 1 skipped (was 97 + 1 baseline; +7).

## 10-Gate Ownership Map — `main_daily` (actual file:line per gate)

| Gate | Owning file | Owning function | Line(s) — where the gate is set |
|---|---|---|---|
| `scraped` | `src/main.py` | `actor_main` | 417 |
| `scraped` | `src/main.py` | `_run_scrape_pipeline` | 1825 |
| `seen_ids_deduped` | `src/main.py` | `actor_main` | 418 |
| `seen_ids_deduped` | `src/main.py` | `_run_scrape_pipeline` | 1826 |
| `county_filtered` | `src/enrichment_pipeline.py` | `run_enrichment_pipeline` | 398 (post-dedup pass-through — scraper enforces upstream via `is_target_county`) |
| `parsed` | `src/enrichment_pipeline.py` | `run_enrichment_pipeline` | 413 (count of `owner_name`-populated post-vacant-filter) |
| `tier_gated` | `src/enrichment_pipeline.py` | `run_enrichment_pipeline` | 454 (post-entity-filter pass-through — tier filter runs upstream in `_run_scrape_pipeline`) |
| `al_property_enriched` | `src/enrichment_pipeline.py` | `run_enrichment_pipeline` | 584 (count of `parcel_id`-populated post-Step-4b) |
| `smarty_standardized` | `src/enrichment_pipeline.py` | `run_enrichment_pipeline` | 525 (count of `dpv_match_code`-populated post-Step-6) |
| `zillow_enriched` | `src/enrichment_pipeline.py` | `run_enrichment_pipeline` | 674 (count of `estimated_value`-populated post-Step-8) |
| `tracerfy_matched` | `src/full_pipeline.py` | `run_full_pipeline` | 206 (success — `stats["matched"]`); 212 / 216 / 220 (skip-branch zero-fills) |
| `datasift_uploaded` | `src/main.py` | `actor_main` | 481 (post-`write_csv`) |
| `datasift_uploaded` | `src/main.py` | `_run_scrape_pipeline` | 1927 (post-`write_csv`) |

Each gate stamps unconditionally on every code path (success / skip / exception / empty input) so the Slack block always renders the full sequence per D-01 invariant.

## 6-Gate Ownership Map — `benchmark` (additive — WARNING 8)

| Gate | Owning file | Owning function | Line(s) |
|---|---|---|---|
| **FunnelCounter ctor** | `src/benchmark_pipeline_al.py` | `run_pipeline` | 198 (pre-seeded with all 6 gates) |
| `pulled` | `src/benchmark_pipeline_al.py` | `run_pipeline` | 259 (post-`BenchmarkSession.list_cases_in_date_range`) |
| `tier_gated` | `src/benchmark_pipeline_al.py` | `run_pipeline` | 388 (count of `status ∈ {"enriched", "skipped_fiduciary"}` — i.e. cases that survived the ZIP filter, regardless of whether they then routed to fiduciary-skip or obituary spend) |
| `fiduciary_filtered` | `src/benchmark_pipeline_al.py` | `run_pipeline` | 391 (count of `status == "enriched"` — tier-survivors whose petitioner is NOT a fiduciary) |
| `obituary_confirmed` | `src/benchmark_pipeline_al.py` | `run_pipeline` | 399 (count of enriched results whose `obituary_match.confidence ∈ {"high", "medium"}`) |
| `tracerfy_matched` | `src/benchmark_pipeline_al.py` | `prepare_notices` | 824 (success — `stats["matched"]`); 830 / 834 (skip-branch zero-fills) |
| `datasift_uploaded` | `src/benchmark_pipeline_al.py` | `_cli` | 1017 (post-CSV write) / 1020 (zero on no-eligible) |

## `PostScrapeOptions` + `PipelineOptions` Extensions (signatures)

```python
# src/full_pipeline.py — PostScrapeOptions additions
@dataclass
class PostScrapeOptions:
    # ... existing fields unchanged ...
    funnel: "FunnelCounter | None" = None
    rate_tracker: "ServiceRateTracker | None" = None
```

```python
# src/enrichment_pipeline.py — PipelineOptions additions
@dataclass
class PipelineOptions:
    # ... existing fields unchanged ...
    funnel: "FunnelCounter | None" = None
    rate_tracker: "ServiceRateTracker | None" = None
```

Both dataclass field additions default to `None`, so legacy callers (CSV re-import, PDF import, photo import — all of which build their own `PipelineOptions` without supplying a funnel) are byte-identical. The `TYPE_CHECKING`-guarded import keeps the forward-string type reference compile-clean without forcing legacy callers to import observability.

## `scraper.scrape_all` rate_tracker kwarg (resolves 02-03 deferral)

```python
async def scrape_all(
    mode: str = "daily",
    # ... existing positional kwargs ...
    on_search_complete=None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> list[NoticeData]: ...
```

Threaded through `scrape_all → run_search → _scrape_notice → solve_captcha_and_view`. After this lands, the legacy `main.py daily` pipeline's 2Captcha rate stops reading `n/a today` because every CAPTCHA solve attempt records into the same tracker that drives the Slack block's "Service Rates" line.

apn_probate's pure-CLI runs (which invoke `scrape_all` from `apn_probate_pipeline_al.py` rather than from `main.py`) will continue reading `n/a today` until that orchestrator instantiates a tracker and passes it into `scrape_all` itself — which is the apn_probate orchestrator's responsibility, NOT this plan's scope.

## `_post_daily_slack_with_funnel` — the testable Slack seam

```python
# src/main.py:64
def _post_daily_slack_with_funnel(
    notices: list,
    funnel: FunnelCounter,
    rate_tracker: ServiceRateTracker,
    *,
    elapsed_min: float = 0,
    cost_breakdown: dict | None = None,
    upload_result: dict | None = None,
    webhook_url: str | None = None,
) -> bool: ...
```

Both `actor_main` (Apify Actor path) and `_run_scrape_pipeline` (CLI path) route their end-of-run Slack post through this single helper. It builds a 3-block payload (legacy `build_summary` text + `build_funnel_block` + `build_service_rates_block`) and POSTs via `_send_blocks_webhook` in one HTTP call (D-02). `load_rolling_rates` fires BEFORE the blocks build; `save_rolling_rates` fires AFTER a successful send only (D-03 / W6).

The helper exists so the funnel-wiring + slack-payload logic can be tested without driving the full Apify Actor or CLI argparse — the 4 tests in `test_main_daily_funnel.py` call this helper directly with a synthetic 10-gate-stamped funnel and assert the 3-block payload, the send-then-save ordering, and the W6 negative path (failed send must not advance the rolling baseline).

## Webhook Payload Shape — `main_daily`

```json
{
  "text": "<plain-text fallback — the existing build_summary output>",
  "blocks": [
    {"type": "section", "text": {"type": "mrkdwn", "text": "<summary>"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Funnel — main_daily*\n• scraped: 10\n• seen_ids_deduped: 10\n• county_filtered: 10\n• parsed: 10\n• tier_gated: 10\n• al_property_enriched: 8\n• smarty_standardized: 7\n• zillow_enriched: 6\n• tracerfy_matched: 4\n• datasift_uploaded: 6"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Service Rates*\n• 2Captcha: 100% today | 99% 7-day\n• Smarty: 70% today | 92% 7-day\n• Tracerfy: 50% today | 41% 7-day\n• LLM: 100% today | — 7-day"}}
  ]
}
```

(Reconstructed from `test_main_daily_slack_includes_funnel_and_rates_blocks` synthetic fixture: 10 scraped, 8 with parcel, 7 Smarty-matched, 6 Zillow-matched, 4 Tracerfy-matched, 6 CSV.)

## Webhook Payload Shape — `benchmark`

```json
{
  "text": "<plain-text fallback — the existing build_slack_message output>",
  "blocks": [
    {"type": "section", "text": {"type": "mrkdwn", "text": "<benchmark summary>"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Funnel — benchmark*\n• pulled: 10\n• tier_gated: 6\n• fiduciary_filtered: 5\n• obituary_confirmed: 3\n• tracerfy_matched: 4\n• datasift_uploaded: 5"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Service Rates*\n• 2Captcha: n/a today | — 7-day\n• Smarty: n/a today | — 7-day\n• Tracerfy: 80% today | — 7-day\n• LLM: 60% today | — 7-day"}}
  ]
}
```

(Reconstructed from `test_benchmark_slack_includes_funnel_and_rates_blocks` synthetic fixture: 10 cases pulled → 8 property-found → 6 in-tier → 5 non-fiduciary → 3 obit-confirmed → 4 Tracerfy-matched → 5 CSV.)

## Rolling-Rates Ordering Confirmation (D-03 / W6)

Both pipelines obey the ordering invariant:

1. **`load_rolling_rates()`** runs BEFORE the blocks list is built. The summary passed into `build_service_rates_block` therefore reflects yesterday-and-prior days only (not today).
2. **`_send_blocks_webhook(text, blocks)`** runs next.
3. **`save_rolling_rates(rate_tracker.totals())`** runs LAST, **only** when `sent is True`. A Slack-post failure leaves the rolling baseline untouched — a bad run cannot pollute the 7-day window.

The send → save ordering is captured by `test_main_daily_save_rolling_rates_called_after_slack_post` (asserts `call_order == ["send", "save"]` and `totals[ "tracerfy" ] == {"success": 4, "total": 8}`) AND `test_benchmark_save_rolling_rates_called_after_slack_post` (asserts `call_order == ["send", "save"]` and `totals["llm"] == {"success": 3, "total": 5}`).

The W6 negative path (failed send must not save) is captured by the extra `test_main_daily_save_skipped_when_slack_post_fails` test — the 4th test in test_main_daily_funnel.py beyond the 3-test spec.

## send_slack_notification Stays Byte-Identical (Wave 1 / W5 Contract)

`grep -L "send_slack_notification" src/main.py src/benchmark_pipeline_al.py`: both files no longer call `send_slack_notification` from the daily / benchmark Phase 2 paths. The legacy text-only helper stays untouched in `slack_notifier.py` (which this plan does NOT modify — Wave 3 zero-overlap audit) for any caller that still uses it directly (the empty-notice early-return in `_run_scrape_pipeline` continues to call `send_slack_notification([])` since wiring funnel for an empty run adds no signal).

`git diff c421889..HEAD -- src/slack_notifier.py` shows zero lines changed — the additive-only contract across the Slack module is preserved end-to-end through Wave 3.

## `_send_blocks_webhook` Is the Single Blocks-Aware Send Path

After this plan lands, the canonical Wave 3 wiring is:

| Pipeline | Plan | Calls `_send_blocks_webhook`? | Calls `send_slack_notification`? |
|---|---|---|---|
| apn_probate | 02-03 | Yes (in notify_slack Phase 2 branch) | No (Phase 2 path); Yes (legacy text path) |
| pre_probate | 02-03 | Yes (in notify_slack Phase 2 branch) | No (Phase 2 path); Yes (legacy text path) |
| main_daily (Apify) | 02-04 | Yes (via _post_daily_slack_with_funnel) | No (replaced — Phase 2 only) |
| main_daily (CLI) | 02-04 | Yes (via _post_daily_slack_with_funnel) | Only on empty-notices early-return |
| benchmark | 02-04 | Yes (in notify_slack Phase 2 branch) | No (Phase 2 path); Yes (legacy text path) |
| tax_distress | 02-05 (next) | Will use _send_blocks_webhook (already in place) | — |
| code_violation | 02-05 (next) | Will use _send_blocks_webhook (already in place) | — |

There is exactly ONE `_send_blocks_webhook` implementation (in `slack_notifier.py`, added by 02-03); no parallel implementations.

## Tests

| File | Tests | Coverage |
|---|---|---|
| `tests/unit/test_main_daily_funnel.py` | 4 | (1) all 10 D-01 gates populate in canonical order with expected counts; (2) Slack blocks payload contains legacy summary section + Funnel — main_daily section + Service Rates section (D-02); (3) save_rolling_rates fires AFTER _send_blocks_webhook returns True with tracker totals propagation (send → save ordering); (4) W6 negative path — failed _send_blocks_webhook leaves rolling baseline untouched (save_rolling_rates NOT called) |
| `tests/unit/test_benchmark_funnel.py` | 3 | (1) all 6 BENCHMARK_GATES populate with expected counts (10 pulled → 6 in-tier → 5 non-fiduciary → 3 obit-confirmed → 4 Tracerfy → 5 CSV); (2) blocks contain Funnel — benchmark + Service Rates sections; (3) send → save call ordering with tracker totals propagation (Tracerfy 4/5, LLM 3/5) |

All offline — no real Slack HTTP, no real Anthropic, no real Benchmark Web, no real Jefferson API, no real Tracerfy. `observability.STATE_FILE` is monkeypatched to `tmp_path` so the real `output/observability/service_rates.json` is NEVER touched during tests. The `output/observability/` directory does not exist after the test run (verified post-suite).

```bash
python -m pytest tests/unit/test_main_daily_funnel.py tests/unit/test_benchmark_funnel.py -v
# 7 passed in 0.19s
```

**Full unit-test regression** — 104 passed + 1 skipped (baseline 97 + 7 new):

```bash
python -m pytest tests/unit/ -q
# 104 passed, 1 skipped in 3.45s
```

## scraper.py Modification Note (Plan-Checker Observation #2)

`src/scraper.py` was added to this plan's files_modified set during execution (it was NOT in the plan frontmatter's initial files_modified list). Justification: the 02-04-PLAN.md Task 2 action explicitly authorizes this — _"If scraper.scrape_all does NOT yet accept rate_tracker, add the kwarg to scraper.py (additive, default=None — same pattern as plan 02-02's service entry points)"_ — and the 02-03 SUMMARY's "scraper.py threading deferred to plan 02-04" entry confirms this was the deferred location for this work.

Wave-3 file-ownership audit confirms zero overlap: no other Wave 3 plan (02-03 or 02-05) touches `src/scraper.py`. The change is purely additive (keyword-only kwarg with default `None`), so apn_probate / pre_probate / Apify Actor callers that don't supply a tracker remain byte-identical.

## Verification Results

| Check | Result |
|---|---|
| All 7 plan-04 tests pass | PASS |
| Zero Phase-1 / Wave-1 / Wave-2 / 02-03 regression (97 baseline + 7 new = 104 total) | PASS |
| PostScrapeOptions + PipelineOptions both have funnel + rate_tracker fields | PASS (`inspect.signature` check passed) |
| `MAIN_DAILY_GATES` has exactly 10 entries in D-01 order | PASS |
| `BENCHMARK_GATES` has exactly 6 entries in benchmark-additive order | PASS |
| `grep -c "funnel.set" src/main.py` ≥ 3 (scraped + seen_ids_deduped + datasift_uploaded — x 2 entry paths) | 6 |
| `grep -c "funnel.set" src/full_pipeline.py` ≥ 1 (tracerfy_matched + zero-fills) | 4 |
| `grep -c "funnel.set" src/enrichment_pipeline.py` ≥ 6 (county_filtered through zillow_enriched) | 6 |
| `grep -c "funnel.set" src/benchmark_pipeline_al.py` ≥ 6 (pulled through datasift_uploaded + skip-branch zero-fills) | 9 |
| `grep -c "FunnelCounter\|_send_blocks_webhook" src/main.py` ≥ 2 | 10 |
| scraper.scrape_all + run_search + _scrape_notice all accept rate_tracker | PASS |
| `git diff c421889..HEAD -- src/slack_notifier.py` shows zero changes | PASS (file untouched) |
| `output/observability/service_rates.json` NEVER touched during test run | PASS (`output/observability/` directory absent post-run) |

## Deviations from Plan

None — plan executed exactly as written. Each task's `<action>` was implemented one-for-one. The only "expansion" was including `src/scraper.py` in this plan's files_modified set, which the plan itself authorizes (Task 2 action note + Wave-3 file-ownership audit caveat) and the 02-03 SUMMARY explicitly defers to here.

Two minor expansions worth flagging (not deviations — all called out in plan or in apn_probate-pattern parity):
- **4 tests in test_main_daily_funnel.py instead of 3**: added `test_main_daily_save_skipped_when_slack_post_fails` to explicitly verify W6 (failed send leaves baseline untouched). The 3-test spec covers send-then-save ordering; this 4th test covers the negative path. Cheap insurance; parallels the W6 invariant the apn_probate tests don't currently test for.
- **scraper.py rate_tracker threading**: as documented above, authorized by the plan's Task 2 action note + the 02-03 deferral.

No Rule 1 (bug) / Rule 2 (missing critical functionality) / Rule 3 (blocking issue) auto-fixes were needed; no Rule 4 architectural escalations occurred.

## Authentication Gates

None encountered — all 4 services (2Captcha, Smarty, Tracerfy, LLM) + the Slack webhook + Benchmark Web were mocked at the appropriate boundaries; no real credentials needed during test execution.

## Wave-3 File-Ownership — No Cross-Plan Collisions

| Plan | Files owned | Overlap check |
|---|---|---|
| 02-03 | `src/slack_notifier.py`, `src/apn_probate_pipeline_al.py`, `src/pre_probate_pipeline_al.py`, `tests/unit/test_apn_probate_funnel.py`, `tests/unit/test_pre_probate_funnel.py` | — |
| 02-04 (this plan) | `src/main.py`, `src/full_pipeline.py`, `src/enrichment_pipeline.py`, `src/scraper.py`, `src/benchmark_pipeline_al.py`, `src/benchmark_obituary_match.py`, `tests/unit/test_main_daily_funnel.py`, `tests/unit/test_benchmark_funnel.py` | Zero overlap with 02-03 or 02-05. Imports `_send_blocks_webhook` + `build_funnel_block` + `build_service_rates_block` from `slack_notifier` (added by 02-03; not modified by this plan). |
| 02-05 (next) | `src/tax_distress_pipeline.py`, `src/code_violation_pipeline.py` | Zero overlap with 02-03 or 02-04. Will import `_send_blocks_webhook` + the block builders from `slack_notifier`. |

`_send_blocks_webhook` remains the canonical shared Slack-blocks send path for all Wave 3 pipelines. Plan 02-05 does NOT need to modify `slack_notifier.py`.

## Self-Check: PASSED

- File `src/full_pipeline.py` → FOUND (modified — PostScrapeOptions extended; tracerfy_matched gate stamped)
- File `src/enrichment_pipeline.py` → FOUND (modified — PipelineOptions extended; 6 enrichment-stage gates stamped)
- File `src/scraper.py` → FOUND (modified — rate_tracker kwarg threaded through scrape_all → run_search → _scrape_notice → solve_captcha_and_view)
- File `src/main.py` → FOUND (modified — FunnelCounter + ServiceRateTracker wired through BOTH actor_main + _run_scrape_pipeline; _post_daily_slack_with_funnel helper; MAIN_DAILY_GATES constant)
- File `src/benchmark_pipeline_al.py` → FOUND (modified — FunnelCounter('benchmark') wired; notify_slack bifurcates legacy vs Phase 2 paths; BENCHMARK_GATES constant; D-04 terminal log)
- File `src/benchmark_obituary_match.py` → FOUND (modified — _parse_obituary_for_petitioner accepts rate_tracker; passes required_keys to chat_json; _OBIT_MATCH_REQUIRED_KEYS constant)
- File `tests/unit/test_main_daily_funnel.py` → FOUND (created — 4 tests)
- File `tests/unit/test_benchmark_funnel.py` → FOUND (created — 3 tests)
- Commit `7c93b48` (feat — full_pipeline + enrichment_pipeline wiring) → FOUND
- Commit `7ec6bc9` (feat — main.py + scraper.py + 4 main_daily tests) → FOUND
- Commit `b5212ab` (feat — benchmark wiring + LLM required_keys + 3 benchmark tests) → FOUND
