---
name: 260525-vwk-plan
quick_id: 260525-vwk
phase: quick
plan: 260525-vwk
description: Thread ServiceRateTracker through notice_parser.parse_notice_page() to close the LLM rate gap in legacy main.py daily flow
status: ready_for_execution
files_modified:
  - src/notice_parser.py
  - src/scraper.py
must_haves:
  truths:
    - parse_notice_page() accepts an additive keyword-only `rate_tracker` kwarg (default None) — no breaking change to legacy callers
    - Both internal LLM calls inside parse_notice_page (extract_county_from_notice + extract_with_llm) thread rate_tracker through to llm_parser
    - scraper._scrape_notice passes its existing rate_tracker (already wired for 2Captcha) into parse_notice_page
    - Existing instrumentation in llm_client.py + llm_parser.py is untouched
    - Full pytest suite still 110 passed + 1 skipped
  key_links:
    - src/notice_parser.py:1171 parse_notice_page signature
    - src/notice_parser.py:1045 extract_county_from_notice call site
    - src/notice_parser.py:1252 extract_with_llm call site
    - src/scraper.py:315 parse_notice_page call site (rate_tracker already in scope at line 279/301)
    - Phase 2 carry-forward note in STATE.md (LLM 0/0 service_rates.json)
---

# Plan 260525-vwk — Thread rate_tracker through parse_notice_page

## Goal

Close the LLM service-rate gap surfaced by today's real main.py daily run validation: `service_rates.json` reported `llm: {success: 0, total: 0}` despite ~28 LLM extractions firing. Phase 2 Wave 2 instrumented the LLM entry points, but the call sites inside `parse_notice_page()` weren't threading `rate_tracker=` through.

## 4 edits

| # | File | Line | Edit |
|---|---|---|---|
| 1 | src/notice_parser.py | 1171 | Add `*, rate_tracker: "ServiceRateTracker \| None" = None` to parse_notice_page signature |
| 2 | src/notice_parser.py | 1045 | `extract_county_from_notice(text, api_key)` → add `rate_tracker=rate_tracker` |
| 3 | src/notice_parser.py | 1252 | `extract_with_llm(...)` → add `rate_tracker=rate_tracker` |
| 4 | src/scraper.py | 315 | `parse_notice_page(page, search.county, search.notice_type, llm_api_key)` → add `rate_tracker=rate_tracker` |

## Verify

- `grep -c rate_tracker src/notice_parser.py` ≥ 3 ✓
- `grep parse_notice_page src/scraper.py` shows `rate_tracker=` in the call ✓
- `inspect.signature(parse_notice_page).parameters` contains `rate_tracker` ✓
- `python -m pytest tests/unit/ -q` returns `110 passed, 1 skipped` ✓

## Out of scope

- Live GHA / production validation — requires next daily scrape run
- LLM call sites OUTSIDE the main.py daily flow:
  - `src/photo_importer.py:205` (photo-import path — separate user workflow)
  - `src/benchmark_obituary_match.py:384,569` (benchmark pipeline — already instrumented via 02-04)
  - `src/probate_property_locator.py:387` (already wired via pre_probate)
