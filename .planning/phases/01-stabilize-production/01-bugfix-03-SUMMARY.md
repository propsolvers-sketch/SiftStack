---
phase: 01-stabilize-production
plan: 01-bugfix-03
subsystem: enrichment-pipeline-validator
tags:
  - enrichment
  - validator
  - garbage-ocr
  - regression-test
  - bugfix-03
requirements:
  - BUGFIX-03
dependency_graph:
  requires:
    - tests/unit/__init__.py        # Wave 1 (01-bugfix-02) — package marker
    - tests/unit/conftest.py        # Wave 1 (01-bugfix-02) — sys.path + .env bootstrap
    - src/enrichment_pipeline.py    # exposes _GARBAGE_RE + _validate_records (read-only)
    - src/notice_parser.py          # exposes NoticeData dataclass (read-only)
  provides:
    - regression-test-net:_GARBAGE_RE
    - regression-test-net:_validate_records (numeric-only address path)
  affects: []
tech-stack:
  added: []
  patterns:
    - "Pure-function integration test — _validate_records over an in-memory list[NoticeData]. No monkeypatching, no HTTP, no external services."
    - "Fixture helper _make_notice(**overrides) — sets only the validator-relevant fields on a default-constructed NoticeData; follows CONVENTIONS.md 'empty strings, never None'."
    - "Explicit @pytest.mark.skip for the documented out-of-scope owner_street gap — keeps the gap visible in pytest output instead of letting it silently disappear."
key-files:
  created:
    - tests/unit/test_garbage_address_validator.py
  modified: []
decisions:
  - "Pinned regex literal: r\"^[^a-zA-Z]*$\" — four positive assertions (numeric-only, punctuation-only, empty, whitespace-only) + two negative (real address, letter-only). Reverting the regex to r\"^[^a-zA-Z0-9]*$\" (the original BUGFIX-03 bug form) breaks test_garbage_re_matches_numeric_only AND test_validator_drops_numeric_only_address."
  - "Skip — not delete — test #10 (owner_street garbage on probate). _validate_records does NOT currently apply _GARBAGE_RE to owner_street; making it pass would require a production-source change, out of scope for BUGFIX-03. Leaving it as a documented skip surfaces the gap in pytest output and lets a future hardening ticket flip it on with a one-line removal."
  - "Two-layer testing — six unit tests on the regex shape itself, plus three integration tests on _validate_records end-to-end. Catches all three regression modes: regex literal reverted, regex 'fixed' the wrong way (re-introducing \\d), validator silently stops calling _GARBAGE_RE."
  - "Probate exemption regression test included (test_validator_keeps_probate_with_pr_mailing). The BUGFIX-03 fix is one character (drop 0-9 from the negated class) but it's adjacent to the _NO_PROPERTY_ADDRESS_TYPES carve-out — verifying that carve-out still works prevents BUGFIX-03 collateral damage."
metrics:
  duration_seconds: ~180  # plan-start to commit landed
  tasks_completed: 1
  files_changed: 1
  commit_hash: 17e73db
  completed: 2026-05-23T00:00:00Z
---

# Phase 1 Plan 01-bugfix-03: `_GARBAGE_RE` Numeric-Only Address Validator Regression Net Summary

**One-liner:** Nine offline pytest guards (plus one documented skip) that fail loudly if the BUGFIX-03 fix to `_GARBAGE_RE` (drop `0-9` from the negated character class so the regex matches its docstring intent: any string with zero letters) ever regresses — either at the regex literal or by the validator silently no-longer-calling it.

## What landed

| Commit | Files |
|--------|-------|
| `17e73db` | `tests/unit/test_garbage_address_validator.py` (+228 lines) |

No production source modified. `git diff --stat src/` returned empty.

## The regex literal pinned

```python
# src/enrichment_pipeline.py:235
_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")
```

The character class negates **letters only**. Any string with zero letters matches (= garbage):
- numeric-only (`"12345"` — the canonical BUGFIX-03 case, a leaked parcel ID)
- punctuation-only (`"---"`)
- empty (`""`)
- whitespace-only (`"   "`)

Any string with at least one letter does NOT match (= keep):
- `"5100 Stokely Lane"`
- `"Main St"`

This matches the docstring intent at `src/enrichment_pipeline.py:230-234`. The original buggy form `r"^[^a-zA-Z0-9]*$"` excluded digits from the negation and therefore failed to recognize numeric-only addresses as garbage — they passed validation and shipped to DataSift as unmailable noise.

## Why this plan was test-only

Per Phase 1 CONTEXT.md and a source audit at planning baseline, the BUGFIX-03 production fix is already in place at `src/enrichment_pipeline.py:235`. The bug class — numeric-only OCR'd parcel-leak addresses passing validation — is **silent in production**: postcards get mailed to invalid addresses; the operator only notices when the return-to-sender pile arrives weeks later. The test net is the only thing that pins the fix against a future refactor that reverts the one-character change.

## The 10 truths and how each maps to the BUGFIX-03 bug class

| # | Test | Truth | Bug class caught |
|---|------|-------|------------------|
| 1 | `test_garbage_re_matches_numeric_only` | `_GARBAGE_RE.match("12345")` is truthy | **PRIMARY GUARD** — the canonical BUGFIX-03 case. Original regex `^[^a-zA-Z0-9]*$` did NOT match `"12345"` because digits were excluded from the negation. Reverting the regex breaks this test. |
| 2 | `test_garbage_re_matches_punctuation_only` | `"---"` matches | Sanity floor — both the original AND fixed regex match this; ensures we didn't over-correct and stop matching pure-punctuation garbage. |
| 3 | `test_garbage_re_matches_empty` | `""` matches | Sanity floor — same as above. |
| 4 | `test_garbage_re_matches_whitespace_only` | `"   "` matches | Sanity floor — same as above. |
| 5 | `test_garbage_re_does_not_match_real_address` | `"5100 Stokely Lane"` does NOT match | Negative pin — catches an over-aggressive regex that accidentally matches real addresses (e.g. anything containing a digit). |
| 6 | `test_garbage_re_does_not_match_letter_only` | `"Main St"` does NOT match | Negative pin — confirms the fix is ASYMMETRIC about letters, not digits. |
| 7 | `test_validator_drops_numeric_only_address` | `_validate_records([NoticeData(address="12345", ...)])` returns `[]` | **PRODUCTION-BEHAVIOR REGRESSION TEST** — catches the third regression mode: someone removes the `_GARBAGE_RE.match()` call from `_validate_records` during a refactor. Unit-level test #1 wouldn't catch that. |
| 8 | `test_validator_keeps_letter_containing_address` | `_validate_records([NoticeData(address="5100 Stokely Lane", ...)])` returns the record | Anchors the positive case — guards against an over-aggressive validator that drops real records. |
| 9 | `test_validator_keeps_probate_with_pr_mailing` | `_validate_records([NoticeData(notice_type="probate", address="", owner_street="123 Main St", ...)])` returns the record | **CARVE-OUT REGRESSION TEST** — the BUGFIX-03 fix is one character and lives adjacent to the `_NO_PROPERTY_ADDRESS_TYPES = {"probate", "divorce"}` exemption. This test verifies the fix did not break the exemption. |
| 10 | `test_validator_drops_probate_with_garbage_owner_street` | `_validate_records` would drop a probate record with `owner_street="99999"` | **SKIPPED** — see "Known gaps / follow-up" below. |

## Known gaps / follow-up

**Test #10 is intentionally skipped.** The current `_validate_records` only applies `_GARBAGE_RE` to `n.address`, not to `n.owner_street`. A probate record whose PR mailing path has garbage in `owner_street` (e.g. `"99999"`) currently passes validation and ships to DataSift — the same bug class BUGFIX-03 fixed for the property-address slot, but on the PR-mailing slot.

**Recommendation: file a follow-up ticket** ("BUGFIX-03b — symmetric owner_street garbage validation") for v2 hardening. The fix is a one-line addition in `_validate_records`'s probate branch (`src/enrichment_pipeline.py` lines ~256-282):

```python
has_pr_mailing = bool(
    n.owner_street.strip() and n.owner_zip.strip()
    and not _GARBAGE_RE.match(n.owner_street)   # ← add this line
)
```

When that lands, the skip decorator on `test_validator_drops_probate_with_garbage_owner_street` can be removed in the same commit and the test will pass — turning the documented gap into pinned behavior with a one-line change in the test file too.

Leaving the test in place (as a skip) rather than deleting it keeps the gap visible in pytest output, anchors the recommended truth as code rather than prose, and removes friction when the follow-up ticket is picked up.

## Test design notes

- **All offline.** Zero HTTP, zero external services. `_validate_records` is pure-function over an in-memory `list[NoticeData]` — no monkeypatching needed.
- **Fixture helper `_make_notice(**overrides)`.** Sets only the fields `_validate_records` reads (notice_type, address, city, zip, date_added, state, received_date) on a default-constructed `NoticeData`. Caller overrides whatever's under test. Follows CONVENTIONS.md "Empty strings, never None" — `NoticeData()` already defaults every field to `""`; the helper just overlays defaults for non-probate validation requirements.
- **`owner_name` populated for probate fixtures.** `_validate_records` line ~281 requires `owner_name.strip()` for probate records. The fixture in test #9 explicitly sets it; without this, the test would have failed for the wrong reason (missing `owner_name`, not BUGFIX-03 regression).
- **Plan's prescribed test pattern followed verbatim.** The plan's `<behavior>` block listed 10 tests with one-sentence descriptions and explicit fixture shapes; implementation lifted those into per-test docstrings that map each test to its BUGFIX-03 bug class (mirrors the convention established in plan 01-bugfix-01's SUMMARY).

## Verification results

```
$ python -m pytest tests/unit/test_garbage_address_validator.py -v
============================== test session starts ==============================
collected 10 items

tests/unit/test_garbage_address_validator.py::test_garbage_re_matches_numeric_only PASSED            [ 10%]
tests/unit/test_garbage_address_validator.py::test_garbage_re_matches_punctuation_only PASSED        [ 20%]
tests/unit/test_garbage_address_validator.py::test_garbage_re_matches_empty PASSED                   [ 30%]
tests/unit/test_garbage_address_validator.py::test_garbage_re_matches_whitespace_only PASSED         [ 40%]
tests/unit/test_garbage_address_validator.py::test_garbage_re_does_not_match_real_address PASSED     [ 50%]
tests/unit/test_garbage_address_validator.py::test_garbage_re_does_not_match_letter_only PASSED      [ 60%]
tests/unit/test_garbage_address_validator.py::test_validator_drops_numeric_only_address PASSED       [ 70%]
tests/unit/test_garbage_address_validator.py::test_validator_keeps_letter_containing_address PASSED  [ 80%]
tests/unit/test_garbage_address_validator.py::test_validator_keeps_probate_with_pr_mailing PASSED    [ 90%]
tests/unit/test_garbage_address_validator.py::test_validator_drops_probate_with_garbage_owner_street SKIPPED [100%]

========================= 9 passed, 1 skipped in 0.05s =========================
```

Phase-level checks (from `<verification>` block):

```
$ grep -nE '_GARBAGE_RE\s*=\s*re\.compile\(r"\^\[\^a-zA-Z\]\*\$"\)' src/enrichment_pipeline.py
235:_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")             ← fix confirmed in place

$ git diff --stat src/
(empty — no production source modified)
```

All success criteria from the plan's `<success_criteria>` block satisfied:

1. ✓ `tests/unit/test_garbage_address_validator.py` exists with 10 test functions (9 PASS + 1 SKIP documented).
2. ✓ All non-skipped tests pass against current src/.
3. ✓ Offline + pure-function (no monkeypatch, no HTTP).
4. ✓ The fix `r"^[^a-zA-Z]*$"` is pinned by 4 positive assertions (numeric-only, punctuation-only, empty, whitespace-only).
5. ✓ Real-address negatives (`"5100 Stokely Lane"`, `"Main St"`) pinned as non-garbage (2 assertions).
6. ✓ Probate exemption regression-tested via test #9 — the BUGFIX-03 fix did not break the `_NO_PROPERTY_ADDRESS_TYPES` carve-out.

## Deviations from Plan

**None.** The plan's `<action>` block prescribed the test pattern, the fixture helper shape, the section banners (`# ── Unit: _GARBAGE_RE ──` / `# ── Integration: _validate_records ──`), and the skip rationale for test #10 precisely — implementation matched it directly. The only additions on top of the prescribed pattern were per-test docstrings mapping each test to its BUGFIX-03 bug class (lifted from the plan's `<behavior>` prose) — mirrors the documentation convention established by plan 01-bugfix-01.

No bugs encountered (Rule 1). No missing critical functionality (Rule 2). No blockers (Rule 3). No architectural questions (Rule 4). No auth gates.

## TDD Gate Compliance

Plan declared `tdd="true"` for Task 1. Same situation as plan 01-bugfix-01: this is a regression-test-only plan — the production fix already shipped at `src/enrichment_pipeline.py:235`, so the canonical RED→GREEN cycle does not apply (tests are expected to pass on first run). Documented as intentional in Phase 1 CONTEXT.md: "If a test fails on first run against the current code, that is the signal that the fix has regressed — surface that to the user before 'fixing' the test."

The single commit `17e73db` is a `test(...)` commit — appropriate for "regression net only" work. No paired `feat(...)` commit follows because no implementation change is needed.

To prove the guards work (manual verification — not done as part of this plan but available for any reviewer): temporarily revert `_GARBAGE_RE` to `r"^[^a-zA-Z0-9]*$"` and rerun — at least `test_garbage_re_matches_numeric_only` and `test_validator_drops_numeric_only_address` must fail.

## Self-Check: PASSED

Verified:
- ✓ FOUND: `tests/unit/test_garbage_address_validator.py` (228 lines)
- ✓ FOUND: commit `17e73db` in `git log --oneline`
- ✓ 9/9 non-skipped pytest tests pass; 1 skip (documented out-of-scope owner_street gap)
- ✓ Fix confirmed in production source: `_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")` at line 235
- ✓ No production source modified (`git diff --stat src/` empty)
- ✓ No accidental file deletions in the commit (post-commit deletion check returned "OK: no deletions")
- ✓ Pre-existing untracked files (`.gitignore`, datasift screenshots, prior-wave SUMMARY.md files) correctly left untouched per environment note
