---
phase: 02-funnel-transparency
plan: 02-02
subsystem: observability
tags:
  - observability
  - instrumentation
  - services
  - rate-tracking
requirements:
  - OBS-01
dependency_graph:
  requires:
    - "src/observability.py::ServiceRateTracker (Wave 1 / plan 02-01)"
    - "src/observability.py::TRACKED_SERVICES (Wave 1 / plan 02-01)"
  provides:
    - "src/captcha_solver.py::solve_captcha_and_view(rate_tracker=)"
    - "src/address_standardizer.py::standardize_addresses(rate_tracker=)"
    - "src/address_standardizer.py::smarty_zip_for_assuranceweb_address(rate_tracker=)"
    - "src/address_standardizer.py::smarty_zip_for_madison_address(rate_tracker=)"
    - "src/address_standardizer.py::smarty_zip_for_marshall_address(rate_tracker=)"
    - "src/llm_client.py::chat_json(rate_tracker=, required_keys=)"
    - "src/llm_client.py::chat_json_async(rate_tracker=, required_keys=)"
    - "src/llm_parser.py::extract_with_llm(rate_tracker=)"
    - "src/llm_parser.py::extract_county_from_notice(rate_tracker=)"
    - "src/llm_parser.py::auto_detect_notice_type(rate_tracker=)"
    - "src/llm_parser.py::_required_keys_for(notice_type)"
    - "src/llm_parser.py::_FORECLOSURE_REQUIRED / _PROBATE_REQUIRED / _EVICTION_REQUIRED / _CODE_VIOLATION_REQUIRED / _DIVORCE_REQUIRED / _AUTO_DETECT_REQUIRED (sorted tuples)"
    - "src/tracerfy_skip_tracer.py::batch_skip_trace(rate_tracker=)"
    - "src/tracerfy_skip_tracer.py::_record_tracerfy_outcomes (helper)"
  affects:
    - "Wave 3 pipeline orchestrators (02-03 / 02-04 / 02-05) — can now pass a single ServiceRateTracker into a pipeline run and have all 4 services contribute to the per-run + 7-day rates"
tech_stack:
  added:
    - "typing.TYPE_CHECKING (guarded ServiceRateTracker imports across 5 service modules)"
  patterns:
    - "Additive keyword-only parameter — `*, rate_tracker: 'ServiceRateTracker | None' = None` everywhere, legacy callers byte-identical"
    - "Forward-string type reference so legacy callers that don't import observability still type-check"
    - "Single-source recording via helper (_record_tracerfy_outcomes) for the multi-return-path case"
    - "Defensive 'is not None' guard at every record() site (no try/except on tracker — explicit kwarg contract)"
    - "Sorted-tuple frozen versions of required-keys sets for deterministic missing-keys log + test introspection"
key_files:
  created: []
  modified:
    - "src/captcha_solver.py — 4 record() sites + docstring corrected (tnpublicnotice.com → alabamapublicnotices.com)"
    - "src/address_standardizer.py — 9 record() sites (5 in standardize_addresses + 3 in smarty_zip_for_assuranceweb_address + helper for 1 docstring)"
    - "src/llm_client.py — _record_and_validate() helper + 3 record() sites (success / missing-keys-failure / None-parsed-failure)"
    - "src/llm_parser.py — sets → sorted tuples + _required_keys_for() helper + rate_tracker kwarg on 3 entry points"
    - "src/tracerfy_skip_tracer.py — _record_tracerfy_outcomes() helper + 6 invocations at every post-submission return path"
    - "tests/unit/test_service_rate_instrumentation.py — 18 new tests (3 captcha + 5 smarty + 6 LLM + 4 tracerfy)"
decisions:
  - "D-04 honored verbatim per service. 2Captcha: success = any of 3 attempts cleared the gate (3 record('2captcha', True) sites covering 'notice text visible', 'gate cleared', 'callback auto-submit'), failure = exhausted retries. IP-block bailout and 'content already visible' paths do NOT record (service never invoked)."
  - "Smarty success = candidate.delivery_line_1 non-empty AND state-guard passed. Failure = empty candidates / state-guard rejection / HTTPError (whole-batch HTTPError records one failure per submitted notice). smarty_zip_for_assuranceweb_address records ONE outcome per logical invocation regardless of how many fallback anchors fire — the multi-anchor retry is an internal optimisation, not separate calls."
  - "Tracerfy at BATCH granularity (the most informative aggregate per the user-memory anchor): record `matched` successes + `(submitted - matched)` failures. Zero-submitted batches record nothing (empty batches shouldn't pollute the rate). Single _record_tracerfy_outcomes helper called from 6 return paths for single-source recording."
  - "LLM success = response JSON parsed AND every required_keys present. _required_keys_for(notice_type) helper covers all 8 routed types (foreclosure / tax_sale / tax_delinquent / probate / pre_probate / eviction / code_violation / divorce). extract_county_from_notice passes ('county',); auto_detect_notice_type passes _AUTO_DETECT_REQUIRED. attorney_name and owner_address_source intentionally excluded from probate required_keys (optional fields per existing comment)."
  - "Sets → sorted tuples at module load: _FORECLOSURE_KEYS etc. converted via `tuple(sorted(_X_KEYS))` so iteration order is deterministic in the missing-keys warning log and test assertions."
  - "Backward-compat preserved everywhere: legacy callers (no rate_tracker kwarg, no required_keys kwarg) get identical behavior; the new kwargs are keyword-only with default None; all 73 baseline unit tests still pass."
metrics:
  duration: "~55 min"
  completed: "2026-05-24"
  test_count: 18
  test_count_total_unit: 91
---

# Phase 2 Plan 02: Per-Service Instrumentation Summary

Wired `rate_tracker: ServiceRateTracker | None = None` into the 4 tracked services' entry points (2Captcha, Smarty, Tracerfy, LLM) per CONTEXT.md D-04 failure semantics — 5 source files instrumented across 22 record() call sites, 18 new offline pytest cases (zero real HTTP), legacy callers byte-identical. Wave 3 pipeline-orchestrator plans (02-03 / 02-04 / 02-05) can now thread a single tracker through any run and have all 4 services contribute to the Slack per-run + 7-day rolling rates.

## What Was Built — Signatures

```python
# src/captcha_solver.py
async def solve_captcha_and_view(
    page: Page,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> bool: ...

# src/address_standardizer.py
def standardize_addresses(
    notices: list[NoticeData],
    auth_id: str,
    auth_token: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> list[NoticeData]: ...

def smarty_zip_for_assuranceweb_address(
    situs: str,
    lastline_hint: str = "Huntsville AL",
    anchor_fallbacks: tuple[str, ...] | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str]: ...

def smarty_zip_for_madison_address(situs: str, *, rate_tracker=None) -> tuple[str, str]: ...
def smarty_zip_for_marshall_address(situs: str, *, rate_tracker=None) -> tuple[str, str]: ...

# src/llm_client.py
def chat_json(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
    required_keys: tuple[str, ...] | None = None,
) -> dict | None: ...

def chat_json_async(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
    required_keys: tuple[str, ...] | None = None,
): ...

# src/llm_parser.py
async def extract_with_llm(
    raw_text: str,
    notice_type: str,
    county: str,
    api_key: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> dict: ...

async def extract_county_from_notice(
    raw_text: str,
    api_key: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> str: ...

async def auto_detect_notice_type(
    raw_text: str,
    county: str,
    api_key: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> str | None: ...

# src/tracerfy_skip_tracer.py
def batch_skip_trace(
    notices: list[NoticeData],
    max_signing_traces: int = 5,
    lookup_heir_addresses: bool = True,
    address_lookup_api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> dict: ...
```

## record() Call Sites — Per Service

### 2Captcha (`src/captcha_solver.py`)

| Line | Path                                                          | Outcome |
|------|---------------------------------------------------------------|---------|
| 145  | Callback auto-submit branch — content visible after token inject | success |
| 158  | Primary post-click branch — Notice Content element present      | success |
| 168  | Fallback post-click branch — CAPTCHA message gone               | success |
| 178  | Post-loop exhaustion — "All %d CAPTCHA attempts failed"         | failure |

**Not recorded (service never invoked):** IP-block bailout (line 60-65), "Notice content already visible — no CAPTCHA needed" (line 67-71), wait-for-selector timeout that triggers a retry, missing CAPTCHA_API_KEY early return.

### Smarty (`src/address_standardizer.py`)

| Line | Function                                  | Path                                          | Outcome |
|------|-------------------------------------------|-----------------------------------------------|---------|
| 134  | standardize_addresses                     | SmartyException on batch send (per notice)    | failure |
| 141  | standardize_addresses                     | Generic exception on batch send (per notice)  | failure |
| 150  | standardize_addresses                     | Empty candidates list                         | failure |
| 183  | standardize_addresses                     | State-guard rejection                         | failure |
| 226  | standardize_addresses                     | Matched (delivery_line_1 populated)           | success |
| 524  | smarty_zip_for_assuranceweb_address       | Primary anchor hit                            | success |
| 536  | smarty_zip_for_assuranceweb_address       | Fallback anchor hit                           | success |
| 540  | smarty_zip_for_assuranceweb_address       | All anchors exhausted                         | failure |

**Not recorded (service never invoked):** Missing Smarty credentials early-return, empty notice list early-return, empty-situs early-return on `smarty_zip_for_assuranceweb_address`. Madison + Marshall wrappers delegate to the assuranceweb helper (single recording point preserves the "one record per logical Smarty call" invariant).

### Tracerfy (`src/tracerfy_skip_tracer.py`)

`_record_tracerfy_outcomes(rate_tracker, *, submitted, matched)` helper (lines 192-218) centralizes the per-batch granularity recording. Called from 6 return paths in `batch_skip_trace`:

| Line | Path                                          | Recording                                    |
|------|-----------------------------------------------|----------------------------------------------|
| 354  | 402 Insufficient Credits                      | `submitted` failures (matched=0)             |
| 366  | Missing queue_id in response                  | `submitted` failures (matched=0)             |
| 421  | Queue status='failed'                         | `submitted` failures (matched=0)             |
| 440  | Successful completion                         | `matched` successes + `(submitted-matched)` failures |
| 449  | Polling timeout (5 min)                       | `submitted` failures (matched=0)             |
| 456  | Generic exception in submit/poll              | `submitted` failures (matched=0)             |

**Not recorded:** Missing Tracerfy API key (service not invoked), empty notice list (submitted=0 → helper short-circuits), no eligible contacts (`if not lookup_map`).

### LLM (`src/llm_client.py`)

`_record_and_validate(parsed, *, rate_tracker, required_keys)` helper (lines 27-58) emits exactly ONE record per call:

| Line | Path                                                  | Outcome |
|------|-------------------------------------------------------|---------|
| 45   | Backend returned None (parse error / HTTP error / no key) | failure |
| 52   | Parsed dict missing one or more required_keys         | failure |
| 57   | Parsed dict AND all required_keys present             | success |

The helper is called from both `chat_json` (sync) and `chat_json_async` (via inner `_wrap` coroutine) so both code paths share the same instrumentation logic.

## Per-Call required_keys Mapping (`src/llm_parser.py`)

| Caller (file:function)                                       | Prompt                          | required_keys                                                                                                                |
|--------------------------------------------------------------|----------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `extract_with_llm` foreclosure / tax_sale / tax_delinquent   | USER_PROMPT_TEMPLATE             | `_FORECLOSURE_REQUIRED` (address, auction_date, city, mortgage_company, original_lender, owner_name, state, trustee, trustee_file_number, zip) |
| `extract_with_llm` probate / pre_probate                     | PROBATE_PROMPT_TEMPLATE          | `_PROBATE_REQUIRED` (address, case_number, city, decedent_name, granted_date, judge_name, owner_city, owner_name, owner_state, owner_street, owner_zip, state, zip) — attorney_name + owner_address_source intentionally NOT required |
| `extract_with_llm` eviction                                  | EVICTION_PROMPT_TEMPLATE         | `_EVICTION_REQUIRED` (address, amount_owed, case_number, city, filing_date, owner_name, state, zip) |
| `extract_with_llm` code_violation                            | CODE_VIOLATION_PROMPT_TEMPLATE   | `_CODE_VIOLATION_REQUIRED` (address, city, compliance_deadline, owner_name, parcel_id, state, violation_type, zip) |
| `extract_with_llm` divorce                                   | DIVORCE_PROMPT_TEMPLATE          | `_DIVORCE_REQUIRED` (address, case_number, city, owner_name, spouse_name, state, zip) |
| `extract_county_from_notice`                                 | _COUNTY_CLASSIFY_PROMPT          | `("county",)` |
| `auto_detect_notice_type`                                    | AUTO_DETECT_PROMPT_TEMPLATE      | `_AUTO_DETECT_REQUIRED` (confidence, notice_type) |

Sets in source (`_FORECLOSURE_KEYS` etc.) are preserved for the legacy in-function `expected.issubset()` defensive check; the new sorted-tuple constants (`_FORECLOSURE_REQUIRED` etc.) are what `chat_json_async` receives.

`src/pre_probate_pipeline_al.py::_extract_decedent_with_llm` and `src/benchmark_obituary_match.py::_parse_obituary_for_petitioner` are NOT touched by this plan — they're in plans 02-03 / 02-04's `files_modified` lists (Wave 3 pipeline-wiring).

## Tests

| Service  | Test                                                                | What it asserts                                                       |
|----------|----------------------------------------------------------------------|-----------------------------------------------------------------------|
| 2Captcha | `test_captcha_records_success_on_clean_solve`                        | totals()['2captcha'] == {success: 1, total: 1}                        |
| 2Captcha | `test_captcha_records_failure_on_exhausted_retries`                  | totals()['2captcha'] == {success: 0, total: 1}                        |
| 2Captcha | `test_captcha_noop_when_rate_tracker_is_none`                        | No exception with legacy call OR explicit None                        |
| Smarty   | `test_smarty_records_one_success_per_resolved_address`               | 2 notices, 1 with delivery_line_1 → totals == {success: 1, total: 2}  |
| Smarty   | `test_smarty_records_failure_on_http_error`                          | SmartyException on send → 3 failures for 3 submitted notices          |
| Smarty   | `test_smarty_zip_assuranceweb_records_success_when_zip_returned`     | (city, zip) returned → 1 success                                      |
| Smarty   | `test_smarty_zip_assuranceweb_records_failure_when_no_match`         | All fallbacks miss → 1 failure                                        |
| Smarty   | `test_smarty_noop_when_rate_tracker_is_none`                         | Both legacy + explicit None paths byte-identical                      |
| LLM      | `test_llm_records_success_when_required_keys_present`                | parsed has all required keys → 1 success                              |
| LLM      | `test_llm_records_failure_when_required_keys_missing`                | parsed missing a required key → 1 failure, return None                |
| LLM      | `test_llm_records_failure_on_json_parse_error`                       | Invalid-JSON response → 1 failure, return None                        |
| LLM      | `test_llm_records_failure_on_http_error`                             | Anthropic SDK raises → 1 failure, return None                         |
| LLM      | `test_llm_extract_with_llm_uses_probate_required_keys`               | extract_with_llm passes _PROBATE_REQUIRED tuple including decedent_name + case_number, excluding attorney_name |
| LLM      | `test_llm_noop_when_rate_tracker_is_none`                            | Tracker untouched when None                                           |
| Tracerfy | `test_tracerfy_records_matched_as_successes_and_unmatched_as_failures` | 5 matched / 12 submitted → totals == {success: 5, total: 12}        |
| Tracerfy | `test_tracerfy_records_all_failures_on_http_error`                   | requests.post raises → 8 failures for 8 submitted                     |
| Tracerfy | `test_tracerfy_zero_submitted_records_nothing`                       | Empty notice list → no record() calls                                 |
| Tracerfy | `test_tracerfy_noop_when_rate_tracker_is_none`                       | Tracker untouched when None                                           |

**Total instrumentation tests:** 18 (plan asked for ≥14). All offline — patches cover `TwoCaptcha` SDK, Smarty `_build_client` + `Batch` + `_smarty_lookup_once`, `anthropic.Anthropic` + `llm_client.chat_json_async`, `requests.post` + `requests.get` + `time.sleep`.

```bash
python -m pytest tests/unit/test_service_rate_instrumentation.py -v
# 18 passed in 3.35s
```

**Full unit-test regression check** — 91 passed + 1 skipped (baseline 73 + 18 new):

```bash
python -m pytest tests/unit/ -q
# 91 passed, 1 skipped in 3.37s
```

## Verification Results

| Check                                                                                                  | Result                                            |
|--------------------------------------------------------------------------------------------------------|---------------------------------------------------|
| All 18 instrumentation tests pass                                                                       | PASS                                              |
| Zero Phase-1 / Wave-1 test regression (73 baseline + 18 new = 91 total)                                 | PASS                                              |
| Backward-compat smoke: all 5 entry-point signatures have `rate_tracker` kwarg with default `None`       | PASS                                              |
| LLM entry points (extract_with_llm + extract_county_from_notice + auto_detect_notice_type) plumb `rate_tracker` | PASS                                      |
| No real service URLs (`tracerfy.com`, `api.2captcha`, `api.smartystreets`, `api.anthropic`) in test file | PASS                                              |
| `grep -c "rate_tracker.record" src/captcha_solver.py` ≥ 2                                               | 4 sites                                           |
| `grep -c 'rate_tracker.record.*smarty'` in src/address_standardizer.py ≥ 3                              | 8 sites                                           |
| `grep -c 'rate_tracker.record.*llm'` in src/llm_client.py ≥ 3                                           | 3 sites                                           |
| `grep -c 'rate_tracker.record.*tracerfy'` in src/tracerfy_skip_tracer.py ≥ 1                            | 2 (in helper, called from 6 return paths)         |

## Deviations from Plan

**One small additive correction (Rule 2 — auto-add missing critical functionality):**

**[Rule 2 - Docstring] Corrected captcha_solver.py module docstring stale reference**
- **Found during:** Task 1 — initial edit was rejected by the user with the note "edit needs to reflect Alabama resources not TN"
- **Issue:** Module docstring read `"2Captcha integration for solving reCAPTCHA v2 on tnpublicnotice.com."` but the active SiftStack pipelines (Jefferson + Madison + Marshall AL) all run on `alabamapublicnotices.com`. The TN reference was a stale Knox/Blount-era artifact.
- **Fix:** Updated the module docstring to read `"2Captcha integration for solving reCAPTCHA v2 on alabamapublicnotices.com."` with sub-paragraph explicitly listing Jefferson / Madison / Marshall as the active AL pipelines.
- **Files modified:** src/captcha_solver.py (docstring only, no logic change)
- **Commit:** e79a533 (same commit as the captcha instrumentation GREEN)

Otherwise the plan executed exactly as written. Each task's `<behavior>` + `<action>` was implemented one-for-one. No Rule 1 (bug) or Rule 3 (blocking-issue) auto-fixes needed; no Rule 4 architectural escalations occurred.

## Authentication Gates

None encountered — all 4 services were mocked at the SDK / HTTP layer, no real credentials needed during test execution.

## Self-Check: PASSED

- File `src/captcha_solver.py` → FOUND (modified)
- File `src/address_standardizer.py` → FOUND (modified)
- File `src/llm_client.py` → FOUND (modified)
- File `src/llm_parser.py` → FOUND (modified)
- File `src/tracerfy_skip_tracer.py` → FOUND (modified)
- File `tests/unit/test_service_rate_instrumentation.py` → FOUND (created)
- Commit `a04e801` (test RED — 2Captcha) → FOUND
- Commit `e79a533` (feat GREEN — 2Captcha) → FOUND
- Commit `4c7953b` (test RED — Smarty) → FOUND
- Commit `a5fa08c` (feat GREEN — Smarty) → FOUND
- Commit `53e9502` (test RED — LLM) → FOUND
- Commit `724e5e8` (feat GREEN — LLM) → FOUND
- Commit `705e0d8` (test RED — Tracerfy) → FOUND
- Commit `a0c8c37` (feat GREEN — Tracerfy) → FOUND
