---
phase: 02-funnel-transparency
plan: 02-03
subsystem: observability
tags:
  - observability
  - pipelines
  - slack
  - funnel
  - wave-3
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
    - "src/llm_client.py::chat_json(rate_tracker=, required_keys=) (Wave 2 / plan 02-02)"
    - "src/address_standardizer.py::smarty_zip_for_madison_address(rate_tracker=) (Wave 2 / plan 02-02)"
    - "src/address_standardizer.py::smarty_zip_for_marshall_address(rate_tracker=) (Wave 2 / plan 02-02)"
    - "src/tracerfy_skip_tracer.py::batch_skip_trace(rate_tracker=) (Wave 2 / plan 02-02)"
  provides:
    - "src/slack_notifier.py::_send_blocks_webhook(text, blocks, webhook_url=None) — appended, additive only"
    - "src/apn_probate_pipeline_al.py::run_pipeline(..., funnel=, rate_tracker=) → (results, funnel, rate_tracker)"
    - "src/apn_probate_pipeline_al.py::prepare_notices(..., funnel=, rate_tracker=)"
    - "src/apn_probate_pipeline_al.py::notify_slack(..., funnel=, rate_tracker=)"
    - "src/pre_probate_pipeline_al.py::_extract_decedent_with_llm(..., rate_tracker=)"
    - "src/pre_probate_pipeline_al.py::run_pipeline(..., funnel=, rate_tracker=) → (results, funnel, rate_tracker)"
    - "src/pre_probate_pipeline_al.py::prepare_notices(..., funnel=, rate_tracker=)"
    - "src/pre_probate_pipeline_al.py::notify_slack(..., funnel=, rate_tracker=)"
  affects:
    - "Wave 3 plans 02-04 + 02-05 — both consume _send_blocks_webhook via import (this plan owns slack_notifier.py)"
    - "output/observability/service_rates.json — written on every successful per-pipeline Slack post (today's totals advance the rolling window for tomorrow)"
tech_stack:
  added: []
  patterns:
    - "Legacy + Phase-2 path bifurcation in notify_slack — backward-compat preserved (no funnel + no tracker → byte-identical _send_webhook plain-text path; any other combination → block-aware path)"
    - "Rolling-rates ordering enforced: load_rolling_rates BEFORE blocks build, save_rolling_rates AFTER successful _send_blocks_webhook (D-03 / W6)"
    - "Pipelines call _send_blocks_webhook DIRECTLY — send_slack_notification stays byte-identical (W5 resolution)"
    - "Funnel gate derivation from disposition counts (pre_probate end-of-loop) — no per-stage increments needed, set() is idempotent / last-write-wins"
    - "Pipeline return-tuple expansion: (results, funnel, rate_tracker) — callers destructure for downstream stage-stamping (datasift_uploaded in _cli) and notify_slack"
key_files:
  created:
    - "tests/unit/test_apn_probate_funnel.py (3 tests)"
    - "tests/unit/test_pre_probate_funnel.py (3 tests)"
  modified:
    - "src/slack_notifier.py (+53 lines — _send_blocks_webhook appended after build_service_rates_block; existing functions byte-identical)"
    - "src/apn_probate_pipeline_al.py (FunnelCounter('apn_probate', 6 gates) + ServiceRateTracker wired through run_pipeline + prepare_notices + notify_slack + _cli)"
    - "src/pre_probate_pipeline_al.py (FunnelCounter('pre_probate', 9 gates) + ServiceRateTracker wired through _extract_decedent_with_llm + run_pipeline + prepare_notices + notify_slack + _cli)"
decisions:
  - "D-01 honored: each pipeline records its OWN gate sequence — apn_probate has 6 gates, pre_probate has 9. Both pre-seeded in FunnelCounter constructor so Slack blocks always render the full ordered sequence even when a stage emitted zero records."
  - "D-02 honored: ONE Slack message per run (not two). Pipelines build a 3-block payload (existing summary section + funnel block + service-rates block) and POST via _send_blocks_webhook in a SINGLE HTTP call. No threading, no second post."
  - "D-03 honored — rolling-rates ordering: load_rolling_rates() runs BEFORE the blocks build (so today's post shows the baseline computed from yesterday-and-prior days); save_rolling_rates(rate_tracker.totals()) runs AFTER a successful _send_blocks_webhook (so today's totals advance the window for tomorrow's baseline, and a failed send leaves the baseline untouched)."
  - "D-04 honored: every pipeline run emits `logger.info('Funnel (%s): %s', funnel.pipeline_name, dict(funnel.as_ordered_dict()))` at end-of-run from _cli BEFORE notify_slack runs — terminal mirrors Slack regardless of whether --notify-slack is set."
  - "W5 resolution: pipelines call _send_blocks_webhook DIRECTLY. send_slack_notification was NOT modified (no blocks_extra kwarg added). The legacy text-only path through _send_webhook stays byte-identical (plan 02-01's additive-only contract preserved across the Slack module)."
  - "W6 resolution: save_rolling_rates is GUARDED behind `if sent and rate_tracker is not None` — a Slack-post failure or a None tracker means the rolling baseline is NOT touched. Failed runs cannot pollute the 7-day window."
  - "Plan-checker observation #2 honored: scraper.py threading deferred to plan 02-04 (which owns main.py / full_pipeline.py and may need scraper.py changes). apn_probate accepts that 2Captcha rate reads 0/0 in pure-apn_probate runs until 02-04 lands. Code comment in run_pipeline marks the deferral location explicitly."
  - "Legacy backward-compat preserved: notify_slack(...) without funnel + rate_tracker (i.e. both None) routes to the original _send_webhook plain-text path. All callers that don't pass the new kwargs get byte-identical pre-Phase-2 behaviour."
  - "LLM required_keys for _extract_decedent_with_llm = ('is_obituary', 'decedent_full_name'). Both keys must be present in the parsed JSON for chat_json to record success (Wave 2 contract). Optional fields (decedent_city, all_survivors, executor_named, etc.) are not required."
metrics:
  duration: "~30 min"
  completed: "2026-05-24"
  test_count: 6
  test_count_total_unit: 97
---

# Phase 2 Plan 03: Wave 3 — apn_probate + pre_probate Wiring Summary

Wired ``FunnelCounter`` + ``ServiceRateTracker`` through ``apn_probate_pipeline_al.py`` (6-gate D-01 sequence) and ``pre_probate_pipeline_al.py`` (9-gate D-01 sequence); appended ``_send_blocks_webhook`` to ``slack_notifier.py`` (the Wave-3 shared helper that plans 02-04 and 02-05 will import). Both pipelines now post a single Slack message per run containing the existing summary section + funnel block + service-rates block (D-02), with rolling-rates ordering strictly enforced (D-03 / W6: load before blocks build, save after successful Slack post). 6 new offline pytest cases; full unit suite at 97 passed + 1 skipped (was 91 + 1 baseline; +6).

## `_send_blocks_webhook` Signature + Contract

```python
# src/slack_notifier.py:459
def _send_blocks_webhook(
    text: str,
    blocks: list[dict],
    webhook_url: str | None = None,
) -> bool:
    """Send {text, blocks} payload to the webhook (Phase 2: OPS-03, OBS-01)."""
```

- **Return:** ``True`` on HTTP 200 or 204; ``False`` on missing URL, network error, or non-2xx status. Mirrors ``_send_webhook``'s return contract so callers can treat both interchangeably for success-tracking purposes.
- **Position:** Appended **below** ``build_service_rates_block`` per the plan spec — section header comment ``# ── Phase 2: Block-aware webhook send (OPS-03, OBS-01) ────`` for navigability.
- **Imports:** Reuses module-level ``requests`` + ``os`` — no new top-level imports added.
- **Side effects:** None beyond the HTTP POST.
- **Existing functions byte-identical:** ``send_slack_notification``, ``_send_webhook``, ``notify_error``, ``notify_warning``, ``notify_preflight_failure``, ``build_summary``, ``build_funnel_block``, ``build_service_rates_block`` — diff vs Wave 2 commit ``bfe110e`` shows only the 53-line ``_send_blocks_webhook`` append. Verified via ``git diff bfe110e..HEAD -- src/slack_notifier.py``.

## apn_probate — Gate Wiring Map (D-01 6-gate sequence)

| Gate                       | File:line                                              | How the count is computed                                                                |
|----------------------------|--------------------------------------------------------|------------------------------------------------------------------------------------------|
| **FunnelCounter ctor**     | `src/apn_probate_pipeline_al.py:162`                   | Pre-seeded with all 6 gates so Slack block always renders the full sequence              |
| `scraped`                  | `src/apn_probate_pipeline_al.py:197`                   | `len(notices)` returned from `scrape_all` (post-internal-dedup)                          |
| `seen_ids_deduped`         | `src/apn_probate_pipeline_al.py:198`                   | Same value as `scraped` — scraper handles `seen_ids.json` dedup internally; documented inline |
| `decedent_name_searched`   | `src/apn_probate_pipeline_al.py:329`                   | `sum(1 for r in results if r.notice.parcel_id)` — count of notices with locator match    |
| `tier_gated`               | `src/apn_probate_pipeline_al.py:333`                   | `sum(1 for r in results if r.status == "enriched")` — survived Tier 1/2 ZIP filter        |
| `tracerfy_matched`         | `src/apn_probate_pipeline_al.py:383`                   | `stats.get("matched", 0)` from `batch_skip_trace` (in `prepare_notices`)                  |
| `datasift_uploaded`        | `src/apn_probate_pipeline_al.py:682` (in `_cli`)       | `len(notices)` after `write_datasift_csv` succeeds                                       |
| **notify_slack blocks**    | `src/apn_probate_pipeline_al.py:534–546`               | load_rolling_rates → build blocks list → _send_blocks_webhook → save_rolling_rates       |
| **save_rolling_rates**     | `src/apn_probate_pipeline_al.py:548`                   | Guarded by `if sent and rate_tracker is not None` — D-03 / W6                            |

**`scraper.scrape_all` rate_tracker plumbing** — **deferred to plan 02-04** (which owns `full_pipeline.py` and may need `scraper.py` changes per the plan-checker observation #2). Code comment at `src/apn_probate_pipeline_al.py:189–194` marks the deferral explicitly. Until 02-04 lands, pure-apn_probate runs will report 2Captcha rate as `n/a today | <baseline> 7-day` because no captcha events are recorded into the tracker from this pipeline's invocation path.

## pre_probate — Gate Wiring Map (D-01 9-gate sequence)

| Gate                       | File:line                                              | How the count is computed                                                                            |
|----------------------------|--------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| **FunnelCounter ctor**     | `src/pre_probate_pipeline_al.py:678`                   | Pre-seeded with all 9 gates                                                                          |
| `obits_harvested`          | `src/pre_probate_pipeline_al.py:688`                   | `len(obits)` returned from `harvest_alabama`                                                         |
| `cross_source_deduped`     | `src/pre_probate_pipeline_al.py:942`                   | `len(results) - dropped_duplicate` count                                                              |
| `fetched`                  | `src/pre_probate_pipeline_al.py:943`                   | `sum(1 for r in results if r.obit_fetched)` — non-empty obit text returned                            |
| `llm_extracted`            | `src/pre_probate_pipeline_al.py:944`                   | `sum(1 for r in results if r.extraction is not None)` — LLM returned a valid DecedentExtraction        |
| `dod_gated`                | `src/pre_probate_pipeline_al.py:945`                   | `llm_extracted_count - dropped_stale_dod` (2-year freshness gate per `MAX_DOD_AGE_YEARS`)             |
| `property_matched`         | `src/pre_probate_pipeline_al.py:946`                   | `sum(1 for r in results if r.property_found)` — `_attach_property_for_decedent` succeeded             |
| `tier_gated`               | `src/pre_probate_pipeline_al.py:947`                   | `sum(1 for r in results if r.status == "enriched")` — survived Tier 1/2 ZIP filter                    |
| `tracerfy_matched`         | `src/pre_probate_pipeline_al.py:1074`                  | `stats.get("matched", 0)` from `batch_skip_trace` (in `prepare_notices`)                              |
| `datasift_uploaded`        | `src/pre_probate_pipeline_al.py:1355` (in `_cli`)      | `len(notices)` after `write_datasift_csv` succeeds                                                    |
| **rate_tracker threading** | LLM @ line 322 / Smarty Madison @ line 802 / Smarty Marshall @ line 799 / Tracerfy @ line 1067 | All Wave 2 entry points threaded via `rate_tracker=rate_tracker` kwarg                                 |
| **notify_slack blocks**    | `src/pre_probate_pipeline_al.py:1220–1232`             | load_rolling_rates → build blocks list → _send_blocks_webhook → save_rolling_rates                    |
| **save_rolling_rates**     | `src/pre_probate_pipeline_al.py:1234`                  | Guarded by `if sent and rate_tracker is not None`                                                     |

## Webhook Payload Shape

Every per-pipeline POST to the Slack webhook sends a JSON body of shape:

```json
{
  "text": "<plain-text fallback — the existing build_slack_message output>",
  "blocks": [
    {"type": "section", "text": {"type": "mrkdwn", "text": "<summary>"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Funnel — <pipeline>*\n• <gate>: <count>\n..."}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Service Rates*\n• 2Captcha: ...\n• Smarty: ...\n• Tracerfy: ...\n• LLM: ..."}}
  ]
}
```

`text` is the plain-text fallback Slack renders when the blocks payload fails to render (legacy clients, mobile push notifications, email digests). `blocks` is the rich Block Kit rendering.

## Example Captured Blocks List (from `test_apn_probate_slack_includes_funnel_and_rates_blocks` mocked run)

Reconstructed from the test fixture with a 4-of-6 Tracerfy match scenario + a 7-day rolling baseline of 99% / 92% / 41% / `None` (smarty/2captcha/tracerfy/llm respectively):

```json
[
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Alabama Post-Probate (APN newspaper-pub) — 2026-05-24* (last 7d)\n  scraped: 10  ·  in-tier: 4 (T1: 4  T2: 0) ..."
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Funnel — apn_probate*\n• scraped: 10\n• seen_ids_deduped: 10\n• decedent_name_searched: 8\n• tier_gated: 6\n• tracerfy_matched: 4\n• datasift_uploaded: 4"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Service Rates*\n• 2Captcha: n/a today | 99% 7-day\n• Smarty: n/a today | 92% 7-day\n• Tracerfy: 67% today | 41% 7-day\n• LLM: n/a today | — 7-day"
    }
  }
]
```

Note the visual distinctions enforced by the block builders:
- `n/a today` → no calls of that service were made this run (different from a real 0%)
- `— 7-day` → no historical baseline yet (different from a real 0%)
- `67%` → actual rate (4/6 Tracerfy matches in the fixture)

## Rolling-Rates Ordering Confirmation

Both pipelines obey the D-03 / W6 ordering:

1. **`load_rolling_rates()`** runs at `src/apn_probate_pipeline_al.py:534` and `src/pre_probate_pipeline_al.py:1220`, BEFORE the blocks list is built. The summary passed into `build_service_rates_block` therefore reflects yesterday-and-prior days only (not today).
2. **`_send_blocks_webhook(...)`** runs at `src/apn_probate_pipeline_al.py:546` and `src/pre_probate_pipeline_al.py:1232`.
3. **`save_rolling_rates(rate_tracker.totals())`** runs at `src/apn_probate_pipeline_al.py:548` and `src/pre_probate_pipeline_al.py:1234`, **only** when `sent is True AND rate_tracker is not None`. A Slack-post failure leaves the rolling baseline untouched — a bad run cannot pollute the 7-day window.

`test_apn_probate_save_rolling_rates_called_after_slack_post` and `test_pre_probate_save_rolling_rates_called_after_slack_post` capture call-order into a list and assert `["send", "save"]` for the success path.

## Tests

| File | Tests | Coverage |
|---|---|---|
| `tests/unit/test_apn_probate_funnel.py` | 3 | All 6 D-01 gates with expected counts (10-notice scenario: 10 scraped, 8 with parcel, 6 in-tier, 4 Tracerfy match, 4 CSV) + blocks contain Funnel + Service Rates sections + send→save call ordering |
| `tests/unit/test_pre_probate_funnel.py` | 3 | All 9 D-01 gates with expected counts (10-obit scenario: 1 fetch-fail, 1 LLM rejection, 1 stale DoD, 1 no-property, 2 off-tier, 4 in-tier, 3-of-4 Tracerfy match) + blocks contain Funnel — pre_probate + Service Rates + send→save call ordering + LLM/Tracerfy totals propagate to rolling save |

All offline — no real Slack HTTP, no real Anthropic, no real Smarty, no real Tracerfy. `observability.STATE_FILE` is monkeypatched to `tmp_path` so the real `output/observability/service_rates.json` is NEVER touched during tests.

```bash
python -m pytest tests/unit/test_apn_probate_funnel.py tests/unit/test_pre_probate_funnel.py -v
# 6 passed in 0.12s
```

**Full unit-test regression** — 97 passed + 1 skipped (baseline 91 + 6 new):

```bash
python -m pytest tests/unit/ -q
# 97 passed, 1 skipped in 3.49s
```

## Verification Results

| Check | Result |
|---|---|
| All 6 plan-03 tests pass | PASS |
| Zero Phase-1 / Wave-1 / Wave-2 regression (91 baseline + 6 new = 97 total) | PASS |
| `_send_blocks_webhook` signature is `(text, blocks, webhook_url)` | PASS |
| All existing `slack_notifier` functions still importable | PASS |
| `slack_notifier.py` additive-only diff (zero deletions, zero modifications to existing functions) | PASS — `git diff bfe110e..HEAD -- src/slack_notifier.py` shows only the 53-line append |
| `grep -c "_send_blocks_webhook\|build_funnel_block\|build_service_rates_block" src/apn_probate_pipeline_al.py` ≥ 3 | 7 |
| `grep -c "_send_blocks_webhook\|build_funnel_block\|build_service_rates_block" src/pre_probate_pipeline_al.py` ≥ 3 | 7 |
| Legacy `notify_slack` callers (no funnel + no tracker) get byte-identical text-only path | PASS — explicit branch at `src/apn_probate_pipeline_al.py:526–535` and `src/pre_probate_pipeline_al.py:1206–1215` |
| `save_rolling_rates` guarded by `if sent and rate_tracker is not None` | PASS — verified at apn:547 and pre:1233 |
| `output/observability/service_rates.json` NEVER touched during test run | PASS — every test monkeypatches `observability.STATE_FILE` to `tmp_path` |

## Deviations from Plan

None — plan executed exactly as written. Each task's `<action>` was implemented one-for-one. No Rule 1 (bug) / Rule 2 (missing critical functionality) / Rule 3 (blocking issue) auto-fixes were needed; no Rule 4 architectural escalations occurred.

One small test-fixture adjustment was needed (not a deviation — internal test concern):
- Initial test_apn_probate_funnel_records_all_gates used decedent names like `"Decedent 0", "Decedent 1"...` which `_normalize_decedent_key` collapses to the same key (digits stripped) — only 1 of 10 survived same-person dedup. Fixed by switching to unique alpha-only last names (`Alpha, Bravo, Charlie...`) in the fixture; identical fix applied to pre_probate fixture.

## Authentication Gates

None encountered — all 4 services (2Captcha, Smarty, Tracerfy, LLM) and the Slack webhook were mocked at the appropriate boundaries; no real credentials needed during test execution.

## Wave-3 File-Ownership — No Cross-Plan Collisions

| Plan | Files owned | Overlap check |
|---|---|---|
| 02-03 (this plan) | `src/slack_notifier.py`, `src/apn_probate_pipeline_al.py`, `src/pre_probate_pipeline_al.py`, `tests/unit/test_apn_probate_funnel.py`, `tests/unit/test_pre_probate_funnel.py` | — |
| 02-04 (next) | `src/main.py`, `src/full_pipeline.py`, `src/enrichment_pipeline.py`, `src/benchmark_pipeline_al.py`, `src/benchmark_obituary_match.py`, `src/scraper.py` | Zero overlap with 02-03. Will import `_send_blocks_webhook` from `slack_notifier` (already in place). |
| 02-05 (after) | `src/tax_distress_pipeline.py`, `src/code_violation_pipeline.py` | Zero overlap with 02-03 or 02-04. Will import `_send_blocks_webhook`. |

`_send_blocks_webhook` is now the canonical shared Slack-blocks send path for all Wave 3 pipelines. Plans 02-04 and 02-05 do NOT need to modify `slack_notifier.py`.

## Self-Check: PASSED

- File `src/slack_notifier.py` → FOUND (modified, additive-only)
- File `src/apn_probate_pipeline_al.py` → FOUND (modified)
- File `src/pre_probate_pipeline_al.py` → FOUND (modified)
- File `tests/unit/test_apn_probate_funnel.py` → FOUND (created)
- File `tests/unit/test_pre_probate_funnel.py` → FOUND (created)
- Commit `9783273` (feat — _send_blocks_webhook + apn_probate funnel wiring + 3 tests) → FOUND
- Commit `c421889` (feat — pre_probate funnel wiring + 3 tests) → FOUND
