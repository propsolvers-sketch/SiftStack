---
phase: 01-stabilize-production
plan: 01-parser-01
type: execute
wave: 2
depends_on:
  - 01-bugfix-02
files_modified:
  - tests/unit/test_pr_address_name_first.py
files_read_only:
  - src/notice_parser.py
autonomous: true
requirements:
  - PARSER-01
priority: P1-quality-regression
tags:
  - notice-parser
  - probate
  - pr-mailing-address
  - al-signature-block
  - regression-test
must_haves:
  truths:
    - "PR_ADDRESS_NAME_FIRST_RE matches the canonical AL vertical signature block ('NAME\\nPersonal Representative\\nADDRESS\\nCITY, STATE ZIP') and populates owner_street / owner_city / owner_zip"
    - "PR_ADDRESS_NAME_FIRST_RE also matches the Executor / Executrix / Administrator / Administratrix title variants in the same vertical layout (AL § 43-2 grants letters of any of those titles)"
    - "PR_ADDRESS_RE (the original TN inline format) still matches the legacy 'Personal Representative: NAME, ADDR' format — the new regex is ADDITIVE, not a replacement"
    - "_parse_pr_address() invokes BOTH regexes (PR_ADDRESS_RE first, then PR_ADDRESS_NAME_FIRST_RE as fallback) — the OR chain at notice_parser.py:1982 is the integration point"
    - "_parse_pr_address() correctly assigns owner_state to 'AL' when notice.state is empty (Alabama default) and respects notice.state when set"
    - "_parse_pr_address() is a no-op when notice.notice_type != 'probate' (does not corrupt foreclosure / tax / code-violation records)"
    - "The fixed code does NOT regress the TN-format case — a real Knox/Blount-era PR address still parses correctly so the TN-legacy photo-import path is not broken"
  artifacts:
    - path: "tests/unit/test_pr_address_name_first.py"
      provides: "Golden test that fails if PR_ADDRESS_NAME_FIRST_RE is removed, if _parse_pr_address() stops chaining the two regexes, if the AL signature-block format stops parsing, or if the TN inline format regresses"
      min_lines: 60
  key_links:
    - from: "tests/unit/test_pr_address_name_first.py"
      to: "src/notice_parser.py"
      via: "import PR_ADDRESS_NAME_FIRST_RE, PR_ADDRESS_RE, _parse_pr_address, NoticeData"
      pattern: "PR_ADDRESS_NAME_FIRST_RE|_parse_pr_address"
---

<objective>
Lock in the PARSER-01 fix: added `PR_ADDRESS_NAME_FIRST_RE` at `src/notice_parser.py:635` to handle the AL probate signature-block format where the **name comes BEFORE the title** with only a newline separator between title and address. The legacy `PR_ADDRESS_RE` requires `{3,80}` non-digit characters between the title and the address, which fits the TN inline format (`"Personal Representative: NAME, ADDR"`) but NEVER matches the AL vertical layout:

```
JOHN SMITH
Personal Representative
123 Main St
Birmingham, AL 35203
```

The new regex uses `{0,80}?` (non-greedy zero minimum) so the address can follow the title with just a newline. `_parse_pr_address()` at line 1966 now chains both regexes: `PR_ADDRESS_RE.search(text) or PR_ADDRESS_NAME_FIRST_RE.search(text)`.

This plan ships the regression test that proves:
1. The AL signature-block format parses correctly (the primary PARSER-01 case).
2. The TN inline format still parses (the original PR_ADDRESS_RE has NOT regressed — both formats coexist).
3. All four title variants work (Personal Representative, Executor, Executrix, Administrator, Administratrix).
4. The OR chain in `_parse_pr_address()` is wired correctly — both regexes participate.

Purpose: Without the AL signature-block parser, AL probate records systematically ship to DataSift without PR mailing addresses whenever the LLM fallback is unavailable (ANTHROPIC_API_KEY unset, Haiku rate-limited, or cost-throttled). The deterministic regex fallback eliminates this dependence on a paid AI service for a structural data shape that's mandated by Alabama Code § 43-2-61.

Output: One golden test file in `tests/unit/`, covering 7 truths with both unit-level regex tests AND integration-level `_parse_pr_address()` tests using fixture notice text representing real AL signature blocks.
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
@src/notice_parser.py

<interfaces>
<!-- Key facts so the executor doesn't need to scan 2000+ lines of notice_parser.py -->

From src/notice_parser.py (the fix):

- `PR_ADDRESS_RE` at line ~602 — TN inline format. Anchors on PR title keyword, then `[^0-9]{3,80}` non-digit chars (3 minimum — REQUIRES some text between title and address), then captures (street, city, zip). Accepts both AL and TN state suffixes (bistate via `notice.state`).

- `PR_ADDRESS_NAME_FIRST_RE` at line ~635 — AL vertical signature block. Same title keyword anchor, BUT `[^0-9]{0,80}?` (zero minimum, non-greedy) so just a newline between title and address suffices. Same capture groups: (street, city, zip). Both AL and TN state suffixes accepted so future TN-format-in-vertical-layout edge cases also flow through.

- `_parse_pr_address(notice: NoticeData) -> None` at line ~1966 — the integration point:
    1. Early return if `notice.notice_type != "probate"`.
    2. `text = notice.raw_text.replace("\xa0", " ")` — normalize non-breaking spaces.
    3. `match = PR_ADDRESS_RE.search(text) or PR_ADDRESS_NAME_FIRST_RE.search(text)` — TN inline tried first, AL signature-block as fallback.
    4. On match: street = `_clean_address(group(1))`; if all-caps, title-case it; assign to `notice.owner_street`. city = `_clean_city(group(2))` → owner_city. owner_state = `notice.state or "AL"`. owner_zip = `group(3)`.

- `NoticeData` is the standard mutable dataclass with ~170 string-defaulted fields. To build a fixture: instantiate, set `notice_type = "probate"`, `raw_text = "<the AL signature block>"`, `state = "AL"` (or leave empty to test the AL default).

- `_SUFFIX` regex constant (referenced by both PR_ADDRESS_* regexes) is defined earlier in notice_parser.py and covers standard USPS street suffixes (St, Ave, Rd, Dr, Ln, Blvd, etc.) — do NOT try to redefine; fixtures should use a suffix that matches (e.g. "St", "Lane", "Drive").

Shared test infrastructure already exists from plan 01-bugfix-02:
- `tests/unit/conftest.py` — adds `src/` to sys.path
- `tests/unit/__init__.py` — present
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Golden test for PR_ADDRESS_NAME_FIRST_RE + _parse_pr_address two-regex chain</name>
  <files>tests/unit/test_pr_address_name_first.py</files>

  <behavior>
    The test file must implement at least these cases as separate pytest functions:

    **Unit-level (regex shape on raw text):**

    1. `test_name_first_re_matches_personal_representative` — Apply `PR_ADDRESS_NAME_FIRST_RE.search()` to the canonical AL signature block:
       ```
       JOHN SMITH
       Personal Representative
       123 Main St
       Birmingham, AL 35203
       ```
       Assert the match returns groups: `("123 Main St", "Birmingham", "35203")`.

    2. `test_name_first_re_matches_executor` — Same shape with title `"Executor"`. Match groups: `("456 Oak Ave", "Hoover", "35226")` for fixture text.

    3. `test_name_first_re_matches_executrix` — Same shape with title `"Executrix"`.

    4. `test_name_first_re_matches_administrator` — Same shape with title `"Administrator"`.

    5. `test_name_first_re_matches_administratrix` — Same shape with title `"Administratrix"`.

    6. `test_name_first_re_does_not_match_tn_inline_format` (optional, weaker assertion) — Apply to `"Personal Representative: Jane Doe, 789 Pine St, Knoxville, TN 37918"`. May or may not match (the regex is permissive enough that it could) — if it matches, document why; if not, that's also fine. This test is informational, not a hard guard.

    **Legacy regression guard:**

    7. `test_legacy_pr_address_re_still_matches_tn_inline` — Apply `PR_ADDRESS_RE.search()` to the TN inline format. Assert it matches and captures the same shape. This proves the AL fix did NOT regress TN.

    **Integration-level (_parse_pr_address on NoticeData):**

    8. `test_parse_pr_address_populates_owner_fields_al_signature_block` — Build `NoticeData(notice_type="probate", raw_text=<AL signature block>, state="AL")`. Call `_parse_pr_address(notice)`. Assert `notice.owner_street == "123 Main St"` (or title-cased equivalent if the fixture was uppercase), `notice.owner_city == "Birmingham"`, `notice.owner_state == "AL"`, `notice.owner_zip == "35203"`.

    9. `test_parse_pr_address_no_op_for_non_probate` — Build `NoticeData(notice_type="foreclosure", raw_text=<AL signature block>)`. Call `_parse_pr_address(notice)`. Assert `notice.owner_street == ""` (unchanged — the function early-returns).

    10. `test_parse_pr_address_handles_uppercase_addresses` — Fixture has `"123 MAIN STREET"` (all caps). Assert `owner_street` ends up title-cased (per the fix's `.isupper() → .title()` branch at line ~1986).

    11. `test_parse_pr_address_defaults_state_to_al_when_notice_state_empty` — Build `NoticeData(notice_type="probate", raw_text=<AL signature block>, state="")`. After parsing, assert `owner_state == "AL"` (the `notice.state or "AL"` fallback).

    All tests must be offline (no network, no Playwright). Fixtures are literal Python strings.
  </behavior>

  <action>
    Create `tests/unit/test_pr_address_name_first.py`.

    Module docstring: reference PARSER-01 (CONCERNS.md "`PR_ADDRESS_RE` is name-after-title only" + CONVENTIONS.md "AL probate PR-name extraction"). Explain the bug class: "AL probate notices use a vertical signature block ('NAME\\nTITLE\\nADDRESS') that the original PR_ADDRESS_RE rejects because the regex requires {3,80} non-digit chars between title and address. The fix adds PR_ADDRESS_NAME_FIRST_RE with {0,80}? (zero minimum, non-greedy) and chains the two via `or` in _parse_pr_address."

    Imports:
    ```python
    from __future__ import annotations
    import pytest
    from notice_parser import (
        PR_ADDRESS_NAME_FIRST_RE,
        PR_ADDRESS_RE,
        _parse_pr_address,
        NoticeData,
    )
    ```

    Fixture pattern — embed real-shape signature blocks as multi-line string constants at the top of the file under a `# ── Fixtures ──` banner. Comment each fixture with its source provenance (e.g. `# Shape from CONVENTIONS.md "AL probate PR-name extraction" example`). Per TESTING.md "Test Data" — "paste the real captured upstream output … as a string literal".

    Example fixture:
    ```python
    AL_SIGNATURE_PR = """NOTICE TO CREDITORS

    Letters Testamentary having been granted on the Estate of MARY ANGELA SMITH,
    deceased, on the 15th day of April, 2026, by the Honorable Honorable Allwin Horn,
    Judge of Probate of Jefferson County, Alabama, notice is hereby given that all
    persons having claims against said estate must file the same within six months
    from the date of grant of letters or be barred.

    JOHN SMITH
    Personal Representative
    123 Main St
    Birmingham, AL 35203
    """
    ```

    Fixture for the title-variant tests: parameterize over `["Personal Representative", "Executor", "Executrix", "Administrator", "Administratrix"]` using `@pytest.mark.parametrize` so the same assertion runs against all 5 titles without test-code duplication.

    Integration test fixture builder:
    ```python
    def _make_probate_notice(raw_text: str, *, state: str = "AL") -> NoticeData:
        n = NoticeData()
        n.notice_type = "probate"
        n.raw_text = raw_text
        n.state = state
        return n
    ```

    Use the `# ── ... ──` section banners to split the file into "Fixtures", "Unit: PR_ADDRESS_NAME_FIRST_RE", "Legacy: PR_ADDRESS_RE", "Integration: _parse_pr_address".

    Per CONVENTIONS.md: `from __future__ import annotations` at top, keyword-only args on the fixture builder (`*, state="AL"`).

    Do NOT modify any production source files. The fix is already in place. If a test fails on first run, surface the regression to the user.

    Reuse `tests/unit/conftest.py` from plan 01-bugfix-02.
  </action>

  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack &amp;&amp; python -m pytest tests/unit/test_pr_address_name_first.py -v</automated>
  </verify>

  <done>
    All required tests pass against current src/. (Test #6 is informational and may pass-or-skip without affecting the result.)

    To prove the guards work: temporarily comment out the `or PR_ADDRESS_NAME_FIRST_RE.search(text)` clause in `_parse_pr_address` at notice_parser.py line ~1982 and rerun — at least `test_parse_pr_address_populates_owner_fields_al_signature_block` and all 5 parameterized title tests for `_parse_pr_address` must fail. Restore before completing.

    Additionally: temporarily delete the `PR_ADDRESS_NAME_FIRST_RE` definition (lines 635-650) and rerun — pytest must fail on import with ImportError, which is the ultimate guard that the regex definition has been removed. (This particular sanity check breaks the test file's import, so it's described here as a one-shot manual verification, not part of the automated suite.)

    Test file lives in `tests/unit/`, runs offline, embeds real-shape AL signature blocks as string literals (per TESTING.md convention), and uses `pytest.mark.parametrize` for the 5 title-variant cases.
  </done>
</task>

</tasks>

<verification>
**Phase-level checks for this plan:**
- `python -m pytest tests/unit/test_pr_address_name_first.py -v` shows all PASSED.
- `grep -n "PR_ADDRESS_NAME_FIRST_RE\s*=\s*re\.compile" src/notice_parser.py` returns one match (confirms regex still defined).
- `grep -n "PR_ADDRESS_NAME_FIRST_RE\.search" src/notice_parser.py` returns at least one match (confirms it is still invoked from `_parse_pr_address`).
- `grep -nE 'PR_ADDRESS_RE\.search\(text\)\s*or\s*PR_ADDRESS_NAME_FIRST_RE\.search' src/notice_parser.py` returns one match (confirms the OR chain is intact in the canonical line 1982).
- No production source files modified.
</verification>

<success_criteria>
1. `tests/unit/test_pr_address_name_first.py` exists with at least 10 test functions (1 of which may be parameterized into 5 title variants).
2. All required tests pass against current src/.
3. Tests are offline, embed real-shape signature-block fixtures as string literals, and use `pytest.mark.parametrize` for the title variants.
4. The PARSER-01 fix is now pinned by both unit-level regex assertions AND integration-level `_parse_pr_address` assertions.
5. The legacy TN inline format (`PR_ADDRESS_RE`) is regression-tested — the fix is proven additive, not a replacement.
6. The `_parse_pr_address` no-op-for-non-probate behavior is regression-tested (a future maintainer cannot accidentally make the function corrupt foreclosure/tax/code-violation records).
7. The bug class — "AL signature-block PR addresses are unparseable without an LLM" — is now caught automatically by pytest.
</success_criteria>

<output>
After completion, create `.planning/phases/01-stabilize-production/01-stabilize-production-01-parser-01-SUMMARY.md` documenting:
- The exact AL signature-block shape pinned (multi-line string in the test file)
- Which 5 PR titles are covered via parametrize (Personal Representative, Executor, Executrix, Administrator, Administratrix)
- The OR chain in _parse_pr_address that the integration tests cover
- Any deviations from the plan
</output>
