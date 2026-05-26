---
name: 260525-vwk-summary
quick_id: 260525-vwk
status: complete
date: 2026-05-26
---

# Quick Task 260525-vwk: LLM rate_tracker Plumb — Summary

## Outcome

✅ **Complete.** `parse_notice_page()` now accepts and threads `rate_tracker` through both internal LLM calls, closing the `LLM: n/a today` cell in Phase 2's service-rates Slack block for the legacy main.py daily flow.

## Root cause

Phase 2 Wave 2 instrumented `llm_client.py` and `llm_parser.py` with `rate_tracker` kwargs, but the call sites inside `notice_parser.parse_notice_page` didn't thread the kwarg through. Today's main.py daily run wrote `output/observability/service_rates.json` with `llm: {success: 0, total: 0}` despite ~28 `LLM extracted: ...` log lines firing — because no caller was passing `rate_tracker=` into `extract_county_from_notice` or `extract_with_llm`.

## What changed (4 edits, 2 files)

| File | Line | Change |
|---|---|---|
| `src/notice_parser.py` | 1171 | Added `*, rate_tracker: "ServiceRateTracker \| None" = None` keyword-only kwarg to `parse_notice_page` signature |
| `src/notice_parser.py` | 1045 | `extract_county_from_notice(text, api_key)` → `extract_county_from_notice(text, api_key, rate_tracker=rate_tracker)` |
| `src/notice_parser.py` | 1252 | `extract_with_llm(notice.raw_text, notice_type, county, llm_api_key,)` → `extract_with_llm(..., rate_tracker=rate_tracker,)` |
| `src/scraper.py` | 315 | `parse_notice_page(page, search.county, search.notice_type, llm_api_key)` → `parse_notice_page(..., rate_tracker=rate_tracker)` |

`scraper._scrape_notice` already had `rate_tracker` in scope from plan 02-04 (line 279 signature + line 301 passes to captcha_solver). This change just extends the same tracker downstream into the LLM stages.

## Verification

| Check | Result |
|---|---|
| `grep -c rate_tracker src/notice_parser.py` (was 0, now ≥3) | **3** ✓ |
| `grep parse_notice_page src/scraper.py` includes `rate_tracker=` | ✓ |
| `inspect.signature(parse_notice_page)` includes `rate_tracker` as keyword-only param | ✓ |
| `PYTHONPATH=src python -c "from notice_parser import parse_notice_page"` | imports cleanly |
| `python -m pytest tests/unit/ -q` | **110 passed, 1 skipped in 3.54s** (no regression) |

## What can NOT be verified from this session

- **Live confirmation that LLM rate counter increments** — requires the next main.py daily scrape. After tomorrow's run, expect `output/observability/service_rates.json` to show `llm: {success: N, total: N}` where N is the count of LLM calls fired during the run, and the Slack service-rates block to show `LLM: <pct>% today | — 7-day` (still `—` for 7-day baseline on day 1 of accumulation).

## Closes Phase 2 carry-forward #2

This was carry-forward item 2 of 4 from the Phase 2 VERIFICATION.md flags:

> 2. `code_violation_pipeline.py` has 0 explicit `rate_tracker=` threads — adapters use internal property API paths not the instrumented `address_standardizer` helpers.
> 3. **LLM rate-tracker shows 0/0 despite many LLM calls** — confirmed daily flow not threading rate_tracker.  ← THIS ONE

Remaining Phase 2 carry-forwards (deferred to Phase 3 consolidation):
1. `apn_probate.run_pipeline` doesn't yet pass `rate_tracker` into `scrape_all` (pure-apn_probate CLI runs show "2Captcha: n/a today")
2. `code_violation_pipeline.py` has 0 rate_tracker threads (adapters skip the instrumented helper)
3. Phase 3 service-rate merge logic across pipelines

## Branch

`feat/al-migration-organized` — commits land on top of today's earlier work.
