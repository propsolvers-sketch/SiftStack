---
phase: 01-stabilize-production
plan: 01-parser-01
subsystem: notice-parser
tags:
  - notice-parser
  - probate
  - pr-mailing-address
  - al-signature-block
  - regression-test
requires:
  - 01-bugfix-02 (tests/unit/ scaffold — __init__.py + conftest.py + .venv pytest)
provides:
  - Regression net for PARSER-01 (PR_ADDRESS_NAME_FIRST_RE + _parse_pr_address OR-chain)
  - Pinned shape of AL probate vertical signature block in test fixture form
affects:
  - tests/unit/test_pr_address_name_first.py (new file, 320 lines, 16 cases)
tech-stack:
  added: []
  patterns:
    - "pytest.mark.parametrize for title-variant fan-out (5 titles × 2 test functions = 10 cases + 6 standalone = 16 total)"
    - "Keyword-only argument on _make_probate_notice() per CONVENTIONS.md"
    - "Multi-line fixture template + .format(title=...) for parametrized signature blocks"
key-files:
  created:
    - tests/unit/test_pr_address_name_first.py
  modified: []
decisions:
  - "Added test_parse_pr_address_respects_explicit_notice_state as an 11th test beyond the plan's 10 — pins the `notice.state or 'AL'` ORDER (not just the AL default branch). A maintainer flipping to `'AL' or notice.state` would silently break TN-state input; this test catches it."
  - "Consolidated the 5 standalone title tests (Plan tests #1–#5) into a single parameterized function `test_name_first_re_matches_all_al_titles[title]` rather than 5 near-duplicate functions, per pytest convention and the plan's <action> guidance ('parameterize over [...] using @pytest.mark.parametrize')."
  - "Skipped Plan's optional test #6 (informational TN-inline-does-not-match-AL-regex assertion) — the regex IS permissive enough that PR_ADDRESS_NAME_FIRST_RE could match a TN-inline string, and the plan explicitly tagged this test as 'informational, not a hard guard'. Inclusion would create a confusing must-fail-or-might-pass test that adds no regression-detection value."
metrics:
  duration: "~7 min (read context → sanity-check regexes → write test file → verify → commit → summary)"
  completed: "2026-05-24T04:56:03Z"
  tests_written: 10  # function count; 16 actual cases via parametrize
  tests_passing: 16
  files_modified: 1
  production_source_modified: false
---

# Phase 1 Plan 01-parser-01: AL Signature-Block PR Address Regression Net Summary

Locked in PARSER-01 (`PR_ADDRESS_NAME_FIRST_RE` at `notice_parser.py:635` + OR-chain at `:1982`) with a 320-line offline pytest file pinning the AL vertical-signature-block format end-to-end. All 16 cases pass against the current `src/` baseline. No production source modified.

## Was Built

**Single file:** `tests/unit/test_pr_address_name_first.py` (320 lines, 10 distinct test functions, 16 cases via `pytest.mark.parametrize`).

Module docstring explicitly cites:
- The bug class (AL signature block uses `NAME\nTITLE\nADDRESS` with only a newline separator; legacy `PR_ADDRESS_RE` requires `{3,80}` non-digit chars between title and address)
- The fix (`{0,80}?` non-greedy zero-min in the new regex; `or` clause in `_parse_pr_address`)
- The 6 truths each test pins
- Cross-refs to `.planning/codebase/CONCERNS.md`, `CLAUDE.md` "AL probate PR-name extraction (multi-pattern)", `.planning/REQUIREMENTS.md` PARSER-01

## AL Signature-Block Shape Pinned

The canonical fixture (embedded as `AL_SIGNATURE_BLOCK_TEMPLATE` constant) replicates a real APN Notice-to-Creditors publication per Ala. Code § 43-2-61:

```
NOTICE TO CREDITORS

Letters Testamentary having been granted on the Estate of MARY ANGELA SMITH,
deceased, on the 15th day of April, 2026, by the Honorable Allwin Horn,
Judge of Probate of Jefferson County, Alabama, notice is hereby given that all
persons having claims against said estate must file the same within six months
from the date of grant of letters or be barred.

JOHN SMITH
{title}                         ← parameterized: Personal Representative | Executor | Executrix | Administrator | Administratrix
123 Main St
Birmingham, AL 35203
```

`{title}` is filled by `@pytest.mark.parametrize` over the 5 AL-recognized fiduciary titles. A second fixture `AL_SIGNATURE_PR_UPPERCASE_STREET` uses the same shape but `"123 MAIN STREET"` / `"BIRMINGHAM"` to exercise the `.isupper() → .title()` branch at `notice_parser.py:1986`.

## 5 Titles Covered via Parametrize

| Title                     | Ala. Code grant authority |
| ------------------------- | ------------------------- |
| `Personal Representative` | § 43-2 (generic)          |
| `Executor`                | § 43-2-1 (male, named in will) |
| `Executrix`               | § 43-2-1 (female, named in will) |
| `Administrator`           | § 43-2-42 (male, intestate or no executor) |
| `Administratrix`          | § 43-2-42 (female, intestate or no executor) |

Each title runs through BOTH `test_name_first_re_matches_all_al_titles[title]` (unit-level regex shape) AND `test_parse_pr_address_populates_owner_fields_for_each_al_title[title]` (integration through `_parse_pr_address`).

## OR-Chain Coverage

The integration-level tests (`test_parse_pr_address_populates_owner_fields_for_each_al_title` × 5 titles) all depend on the OR-chain at `notice_parser.py:1982`:

```python
match = PR_ADDRESS_RE.search(text) or PR_ADDRESS_NAME_FIRST_RE.search(text)
```

The first half (`PR_ADDRESS_RE`) cannot match the AL vertical layout (3-char minimum vs 1-char newline separator), so the AL fixture forces the fallback to `PR_ADDRESS_NAME_FIRST_RE`. Removing the `or` clause would cause all 5 integration tests to fail — exactly the regression-detection behavior the plan calls for.

The legacy half is independently regression-tested by `test_legacy_pr_address_re_still_matches_tn_inline` (TN inline format → `PR_ADDRESS_RE` matches) — proves the AL fix is additive, not a replacement.

## Test-by-Test Coverage Map

| Test                                                                                | Truth pinned                                                       | Cases |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ----- |
| `test_name_first_re_matches_all_al_titles[5 titles]`                                | #1, #2 — regex matches AL layout for all fiduciary titles          | 5     |
| `test_name_first_re_captures_uppercase_street_unchanged`                            | Regex preserves source casing (downstream title-casing is separate) | 1     |
| `test_legacy_pr_address_re_still_matches_tn_inline`                                 | #3 — fix is additive; TN PR_ADDRESS_RE not regressed               | 1     |
| `test_parse_pr_address_populates_owner_fields_for_each_al_title[5 titles]`          | #4 — OR-chain wires fallback regex into pipeline for every title   | 5     |
| `test_parse_pr_address_no_op_for_non_probate`                                       | #6 — early-return guards foreclosure/tax/code from corruption      | 1     |
| `test_parse_pr_address_title_cases_uppercase_street`                                | `.isupper() → .title()` branch at notice_parser.py:1986            | 1     |
| `test_parse_pr_address_defaults_state_to_al_when_notice_state_empty`                | #5 — `notice.state or "AL"` default at notice_parser.py:1992      | 1     |
| `test_parse_pr_address_respects_explicit_notice_state`                              | `notice.state or "AL"` ORDER (TN-state input not overwritten)      | 1     |
| **TOTAL**                                                                           |                                                                    | **16** |

## Verification Results

```
tests/unit/test_pr_address_name_first.py ............ 16 passed in 0.05s
```

Plan-level checks (all pass):

| Check                                                                                                      | Result        |
| ---------------------------------------------------------------------------------------------------------- | ------------- |
| `grep -n "PR_ADDRESS_NAME_FIRST_RE\s*=\s*re\.compile" src/notice_parser.py`                                | line 635 ✓    |
| `grep -n "PR_ADDRESS_NAME_FIRST_RE\.search" src/notice_parser.py`                                          | line 1982 ✓   |
| `grep -nE 'PR_ADDRESS_RE\.search\(text\)\s*or\s*PR_ADDRESS_NAME_FIRST_RE\.search' src/notice_parser.py`    | line 1982 ✓   |
| `git diff --stat src/`                                                                                     | empty (clean) ✓ |

## Deviations from Plan

### Decisions documented in frontmatter (none affect plan intent)

1. **Added 11th test (`test_parse_pr_address_respects_explicit_notice_state`)** — pins the ORDER of `notice.state or "AL"` (not just the default branch). Plan called for 10 tests; this is +1 covering an edge case the plan didn't explicitly enumerate but matches truth #5's "respects notice.state when set" half.

2. **Consolidated tests #1–#5 into one parameterized function** — `test_name_first_re_matches_all_al_titles[title]` instead of 5 near-duplicate standalone functions (`test_name_first_re_matches_personal_representative`, `..._executor`, etc.). Plan explicitly recommended `pytest.mark.parametrize` for the title variants. The 5 parameter cases still surface independently in the pytest verbose output, so per-title failure visibility is preserved.

3. **Skipped plan test #6** (informational TN-inline-does-not-match-AL-regex assertion) — plan tagged it "informational, not a hard guard" and "may or may not match". A test whose passing behavior is undefined adds no regression-detection value and would confuse future maintainers; omitted as low-signal.

### Auto-fixed issues (Rules 1–3)

None — the production source was already correct. The plan was regression-test-only and the existing fix at `notice_parser.py:635` + `:1982` was verified intact by the phase-level grep checks.

### Auth gates

None.

## Commit

- `decef4d` — `test(01-parser-01): add PR_ADDRESS_NAME_FIRST_RE + AL signature-block regression net`

## Bug Class Now Caught Automatically

"AL signature-block PR addresses are unparseable without an LLM" — historically the silent failure mode was: `ANTHROPIC_API_KEY` unset → Haiku rate-limited → cost-throttled → AL probates ship to DataSift without PR mailing addresses → enrichment pipeline can't send postcards. The 11 deterministic regex + integration assertions in this file ensure any of the following regressions fails CI immediately:

- Removing or narrowing `PR_ADDRESS_NAME_FIRST_RE`
- Dropping the `or PR_ADDRESS_NAME_FIRST_RE.search(text)` clause from `_parse_pr_address`
- Removing the `notice_type != "probate"` early-return guard
- Flipping `notice.state or "AL"` order (would overwrite TN-state input with AL)
- Removing the `.isupper() → .title()` branch
- Tightening `PR_ADDRESS_RE` in a way that breaks the TN inline legacy path

## Self-Check: PASSED

- [x] `tests/unit/test_pr_address_name_first.py` exists (verified: 320 lines, `git ls-tree HEAD tests/unit/`)
- [x] Commit `decef4d` exists (verified: `git log --oneline -1` → `decef4d test(01-parser-01): add PR_ADDRESS_NAME_FIRST_RE + AL signature-block regression net`)
- [x] All 16 pytest cases pass (verified: `python -m pytest tests/unit/test_pr_address_name_first.py -v` → `16 passed in 0.05s`)
- [x] All 4 phase-level grep / no-source-modified checks pass
- [x] No production source modified (`git diff --stat src/` empty)
