---
phase: 01-stabilize-production
plan: 01-bugfix-03
type: execute
wave: 2
depends_on:
  - 01-bugfix-02
files_modified:
  - tests/unit/test_garbage_address_validator.py
files_read_only:
  - src/enrichment_pipeline.py
  - src/notice_parser.py
autonomous: true
requirements:
  - BUGFIX-03
priority: P1-quality-regression
tags:
  - enrichment
  - validator
  - garbage-ocr
  - regression-test
must_haves:
  truths:
    - "_GARBAGE_RE matches strings containing zero letters (numeric-only, punctuation-only, empty) — these are correctly identified as garbage"
    - "_GARBAGE_RE does NOT match strings containing at least one letter (real addresses like '123 Main St' pass through)"
    - "_validate_records drops a NoticeData whose address is numeric-only (e.g. '12345' — the canonical OCR'd-parcel-leak case BUGFIX-03 was about)"
    - "_validate_records keeps a NoticeData whose address contains both digits and letters (e.g. '5100 Stokely Lane') even if other fields are sparse, provided the other validation checks (city, zip, date format) pass"
    - "Probate / divorce notices are still exempt from the address-required check (the BUGFIX-03 fix is symmetric — the validator's _NO_PROPERTY_ADDRESS_TYPES carve-out is untouched)"
    - "A future regression that changes _GARBAGE_RE back to r'^[^a-zA-Z0-9]*$' (the original buggy form) causes at least one test to fail loudly"
  artifacts:
    - path: "tests/unit/test_garbage_address_validator.py"
      provides: "Golden test for the _GARBAGE_RE regex shape AND the _validate_records integration path; fails if numeric-only addresses ever pass validation again"
      min_lines: 40
  key_links:
    - from: "tests/unit/test_garbage_address_validator.py"
      to: "src/enrichment_pipeline.py"
      via: "import _GARBAGE_RE + _validate_records + construct NoticeData fixtures"
      pattern: "_GARBAGE_RE|_validate_records"
---

<objective>
Lock in the BUGFIX-03 fix (`_GARBAGE_RE` mismatch with its docstring — original regex `^[^a-zA-Z0-9]*$` matched ONLY strings entirely non-alphanumeric, letting numeric-only OCR garbage like a leaked parcel ID `"12345"` pass the validator and ship to DataSift as unmailable noise).

The fix in `src/enrichment_pipeline.py:235` is now `_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")` — matches strings with zero letters, correctly rejecting numeric-only addresses. The comment block at lines 228-234 documents the bug class.

This plan ships the regression test that pins both the regex shape AND the `_validate_records` integration path. The regression test must catch BOTH:
1. The regex being reverted to `r"^[^a-zA-Z0-9]*$"` (the original bug).
2. Someone "fixing" the regex by adding `\d` back to the character class (would re-introduce the bug class).
3. The fix being silently bypassed by `_validate_records` no longer calling `_GARBAGE_RE.match()` (e.g. someone disabling the check during a refactor).

Purpose: Numeric-only addresses passing validation is a low-frequency but persistent quality leak — DataSift sequences fire on these records, postcards mail to invalid addresses, and the operator only notices when the return-to-sender pile arrives weeks later. The test pinpoints the regression at the earliest possible moment.

Output: One golden test file in `tests/unit/`, covering both the unit-level regex behavior AND the integration-level validator behavior on real `NoticeData` instances.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/REQUIREMENTS.md
@.planning/codebase/CONCERNS.md
@.planning/codebase/CONVENTIONS.md
@.planning/codebase/TESTING.md

# The fixed code — read only, do not modify
@src/enrichment_pipeline.py
@src/notice_parser.py

<interfaces>
<!-- Key facts so the executor doesn't have to spelunk the 800+ line enrichment_pipeline -->

From src/enrichment_pipeline.py (the fix, lines ~228-285):
- `_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")` — matches if zero letters present (line ~235).
- `_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")` — used by the same validator, mention it so the test fixtures supply valid YYYY-MM-DD date_added values.
- `_validate_records(notices: list[NoticeData]) -> list[NoticeData]` — runs the address-must-contain-a-letter check via `_GARBAGE_RE.match(notice.address)`. Records that match the garbage regex are dropped with an `invalid_count` increment + a logger warning. Probate / divorce are exempt from the address-required check via `_NO_PROPERTY_ADDRESS_TYPES = {"probate", "divorce"}`.
- The check is symmetric: a record with NO address at all (for probate/divorce) is OK if owner_street + owner_city + owner_zip are populated (PR mailing address). A record with a garbage-string address is dropped regardless of notice_type (a non-empty-but-letterless string is garbage even on probate).

From src/notice_parser.py:
- `NoticeData` is a `@dataclass` with ~170 fields, every field defaults to `""`. To construct a minimal valid fixture: set `address`, `city`, `zip`, `date_added` to plausible values + `notice_type` to "foreclosure". Everything else can default.

The test must NOT depend on Smarty / Zillow / Tracerfy / any external service — `_validate_records` is pure-function over the in-memory list.

Shared test infrastructure already exists from plan 01-bugfix-02:
- `tests/unit/conftest.py` — adds `src/` to sys.path
- `tests/unit/__init__.py` — present
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Golden test for _GARBAGE_RE + _validate_records integration</name>
  <files>tests/unit/test_garbage_address_validator.py</files>

  <behavior>
    The test file must implement at least these cases as separate pytest functions:

    **Unit-level (_GARBAGE_RE shape):**
    1. `test_garbage_re_matches_numeric_only` — `_GARBAGE_RE.match("12345")` is truthy. This is the canonical BUGFIX-03 case (a leaked parcel ID).
    2. `test_garbage_re_matches_punctuation_only` — `_GARBAGE_RE.match("---")` is truthy.
    3. `test_garbage_re_matches_empty` — `_GARBAGE_RE.match("")` is truthy.
    4. `test_garbage_re_matches_whitespace_only` — `_GARBAGE_RE.match("   ")` is truthy.
    5. `test_garbage_re_does_not_match_real_address` — `_GARBAGE_RE.match("5100 Stokely Lane")` is falsy.
    6. `test_garbage_re_does_not_match_letter_only` — `_GARBAGE_RE.match("Main St")` is falsy.

    **Integration-level (_validate_records on NoticeData):**
    7. `test_validator_drops_numeric_only_address` — Construct a `NoticeData(notice_type="foreclosure", address="12345", city="Birmingham", state="AL", zip="35203", date_added="2026-05-01")`. Pass through `_validate_records([notice])`. Assert the returned list is EMPTY (record dropped). This is the regression test for the production behavior BUGFIX-03 fixes.
    8. `test_validator_keeps_letter_containing_address` — Same fixture but `address="5100 Stokely Lane"`. Assert the returned list has length 1 (record kept).
    9. `test_validator_keeps_probate_with_pr_mailing` — Construct a `NoticeData(notice_type="probate", address="", city="", zip="", owner_street="123 Main St", owner_city="Birmingham", owner_state="AL", owner_zip="35203", date_added="2026-05-01")`. Assert the record is kept — probate exemption still works.
    10. `test_validator_drops_probate_with_garbage_owner_street` — Same as #9 but with `owner_street="99999"` (numeric-only). The fix should ALSO catch garbage in the PR mailing path. (If `_validate_records` does not currently check owner_street with _GARBAGE_RE, mark this test with `pytest.skip("future hardening — not in current validator scope")` and document the gap in the SUMMARY. Do NOT modify production code to make it pass — that's a different ticket.)

    All 10 tests must be offline and pure-function. No mocking needed.
  </behavior>

  <action>
    Create `tests/unit/test_garbage_address_validator.py`.

    Module docstring: reference BUGFIX-03 (CONCERNS.md "`_GARBAGE_RE` doesn't do what the docstring says"). Explain the bug class: "the original regex `^[^a-zA-Z0-9]*$` matched ONLY strings with zero alphanumeric characters, which let `'12345'` through as a valid address. The fix `^[^a-zA-Z]*$` requires at least one letter, matching the docstring intent."

    Imports:
    ```python
    from __future__ import annotations
    import pytest
    from enrichment_pipeline import _GARBAGE_RE, _validate_records
    from notice_parser import NoticeData
    ```

    Test pattern for the integration tests:
    ```python
    def _make_notice(**overrides) -> NoticeData:
        # Sensible defaults — all required-for-non-probate fields populated;
        # caller overrides the field under test.
        defaults = dict(
            notice_type="foreclosure",
            address="5100 Stokely Lane",
            city="Birmingham",
            state="AL",
            zip="35203",
            date_added="2026-05-01",
            received_date="2026-05-01",
        )
        defaults.update(overrides)
        n = NoticeData()
        for k, v in defaults.items():
            setattr(n, k, v)
        return n
    ```

    Use the `# ── ... ──` box-drawing section banners to split the file into "Unit: _GARBAGE_RE" and "Integration: _validate_records".

    Per CONVENTIONS.md: empty strings (never None) in fixture defaults, `from __future__ import annotations` at top, lazy `%`-style if any logging is added (not needed for these assertions).

    Do NOT modify any production source files. The fix is already in place. If a test fails on first run, surface the regression to the user before "fixing" the test.

    Reuse `tests/unit/conftest.py` from plan 01-bugfix-02.
  </action>

  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack &amp;&amp; python -m pytest tests/unit/test_garbage_address_validator.py -v</automated>
  </verify>

  <done>
    All 10 tests either pass or are explicitly skipped (test #10 may skip if the validator does not currently check owner_street — that is acceptable per the behavior spec).

    To prove the guards work: temporarily revert `_GARBAGE_RE` to `r"^[^a-zA-Z0-9]*$"` and rerun — at least `test_garbage_re_matches_numeric_only` and `test_validator_drops_numeric_only_address` must fail. Restore before completing.

    Test file lives in `tests/unit/`, runs offline, and uses real `NoticeData` instances (no monkeypatching needed since `_validate_records` is pure-function).
  </done>
</task>

</tasks>

<verification>
**Phase-level checks for this plan:**
- `python -m pytest tests/unit/test_garbage_address_validator.py -v` shows all PASSED (skip on #10 is acceptable if the SUMMARY explains why).
- `grep -nE '_GARBAGE_RE\s*=\s*re\.compile\(r"\^\[\^a-zA-Z\]\*\$"\)' src/enrichment_pipeline.py` returns one match (confirms the fix is in place — the regex literal must exactly contain `[^a-zA-Z]`, not `[^a-zA-Z0-9]`).
- No production source files modified (`git diff --stat src/` shows nothing).
</verification>

<success_criteria>
1. `tests/unit/test_garbage_address_validator.py` exists with 9-10 test functions (10th may be a skip if owner_street is not currently validated).
2. All non-skipped tests pass against current src/.
3. Tests are offline and pure-function — no monkeypatching, no HTTP.
4. The fix `r"^[^a-zA-Z]*$"` is now pinned by at least 4 assertions (numeric-only, punctuation-only, empty, whitespace-only — all must be matched as garbage).
5. Real-address negatives (`"5100 Stokely Lane"`, `"Main St"`) are pinned as non-garbage.
6. The probate exemption is regression-tested via test #9 — the fix to BUGFIX-03 must not have broken the validator's existing `_NO_PROPERTY_ADDRESS_TYPES` carve-out.
</success_criteria>

<output>
After completion, create `.planning/phases/01-stabilize-production/01-stabilize-production-01-bugfix-03-SUMMARY.md` documenting:
- The exact regex literal pinned (`r"^[^a-zA-Z]*$"`)
- Whether test #10 (owner_street garbage check) skipped, and if so, the recommendation to file a follow-up ticket for symmetric owner_street validation
- Any deviations from the plan
</output>
