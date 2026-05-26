---
name: 260525-ucl-summary
quick_id: 260525-ucl
status: complete
date: 2026-05-26
---

# Quick Task 260525-ucl: TN → AL CSV Filename Rename — Summary

## Outcome

✅ **Complete.** The daily output CSV now writes as `al_notices_<timestamp>.csv` instead of `tn_notices_<timestamp>.csv`, matching the AL-pivoted reality of the project.

## What changed (3 atomic edits, 1 atomic commit)

| File | Line | Before | After |
|---|---|---|---|
| `src/data_formatter.py` | 245 | `f"tn_notices_{timestamp}.csv"` | `f"al_notices_{timestamp}.csv"` |
| `src/main.py` | 1118 | CLI help: `tn_notices_<ts>.csv` | `al_notices_<ts>.csv` |
| `src/main.py` | 1923 | Debug log: `"Skipping raw tn_notices CSV (--no-raw-csv set)"` | `"Skipping raw al_notices CSV (--no-raw-csv set)"` |

## Verification

- `grep -nE "tn_notices" src/` returns nothing → sweep is clean
- `python -m pytest tests/unit/ -q` → **110 passed, 1 skipped in 3.59s** (no regression from 73 + 18 + 19 = 110 baseline established in Phases 1 + 2)
- No test files asserted the old filename pattern — filename is generated at write-time, not tested directly

## Out of scope (deferred per user decision)

- **Category B** — Apify Actor manifest (`tn-public-notice-scraper` name, `tn_username`/`tn_password` input keys). Skipped because renaming touches GitHub Actions workflow YAML and Apify Console state.
- **Category C** — CLAUDE.md project framing (Knox/Blount TN references). Skipped — CLAUDE.md docs the photo pipeline + Knox Tax API which are legitimately TN.
- **Category D** — Internal code comments (`config.py:31`, `foreclosure_filter.py:12`, `data_formatter.py:169` Tennessee link-format note). Skipped as low-priority dev-only hygiene.

These remain as candidates for future quick tasks if/when they become irritants. The Apify Actor manifest rename is the next-most-visible one if you decide to do it.

## Effect on tomorrow's run

The next daily scrape will write `output/al_notices_<timestamp>.csv` instead of `output/tn_notices_<timestamp>.csv`. Today's existing `tn_notices_2026-05-25_102123.csv` (and any prior runs) stay on disk with original names as historical record — only NEW writes get the new prefix.

The DataSift CSVs (`datasift_upload_DMs_*.csv` + `datasift_upload_Heirs_*.csv`) were never TN-prefixed and are unaffected.
