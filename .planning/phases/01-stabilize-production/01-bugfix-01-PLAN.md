---
phase: 01-stabilize-production
plan: 01-bugfix-01
type: execute
wave: 2
depends_on:
  - 01-bugfix-02
files_modified:
  - tests/unit/test_search_madison_name_format.py
files_read_only:
  - src/probate_property_locator.py
  - src/madison_property_api.py
autonomous: true
requirements:
  - BUGFIX-01
priority: P1-quality-regression
tags:
  - madison
  - probate
  - property-locator
  - name-format
  - regression-test
must_haves:
  truths:
    - "For an input like 'MARY ANGELA SMITH' (probate-notice format 'FIRST MIDDLE LAST'), _search_madison issues a search with last_name='SMITH', first_name='MARY ANGELA' AND a search with last_name='MARY', first_name='ANGELA SMITH' — both interpretations are tried, not only the second"
    - "For an input like 'SMITH, MARY ANGELA' (comma form), _search_madison issues exactly one search with last_name='SMITH', first_name='MARY ANGELA' (no double-tried last-first ambiguity needed)"
    - "Single-token inputs ('SMITH') call search_by_owner_name('SMITH', None) once"
    - "Empty input returns [] without raising"
    - "Results from both interpretations are deduplicated by parcel_number so a property that matches both queries is not double-counted"
    - "The Jefferson sibling (_search_jefferson) is verified to still try the last-first reorder (regression guard against someone 'simplifying' Jefferson when refactoring Madison)"
  artifacts:
    - path: "tests/unit/test_search_madison_name_format.py"
      provides: "Golden test that fails if _search_madison ever drops the FIRST-MIDDLE-LAST interpretation, double-fires on comma input, breaks dedup, or if _search_jefferson loses its last-first retry"
      min_lines: 40
  key_links:
    - from: "tests/unit/test_search_madison_name_format.py"
      to: "src/probate_property_locator.py"
      via: "monkeypatch search_by_owner_name + invoke _search_madison / _search_jefferson"
      pattern: "search_by_owner_name|_search_madison|_search_jefferson"
---

<objective>
Lock in the BUGFIX-01 fix (Madison `_search_madison` name-format bug — the original implementation passed `(parts[0], parts[1:])` which queried 'FIRST' as last_name and silently degraded Madison probate property-locator hit rate to near-zero).

The fix in `src/probate_property_locator.py:176-232` now tries BOTH interpretations:
- (A) `(parts[-1], parts[:-1])` — probate-notice format 'FIRST MIDDLE LAST'
- (B) `(parts[0], parts[1:])` — assessor / tax-roll format 'LAST FIRST MIDDLE' (the PR-name fallback path)

… and deduplicates by parcel_number. This plan ships the regression test that proves both interpretations fire, the comma form short-circuits correctly, and dedup works. It also adds a parallel guard on `_search_jefferson` so a future "consolidate the two adapters" refactor cannot silently drop Jefferson's last-first retry while patching Madison.

Purpose: Without this test, the Madison hit-rate regression is silent — there is no metric in production that flags "Madison probate-locator returned 0 hits today" because that's also the legitimate result for a true cold day. The test is the only thing standing between a refactor and the bug returning.

Output: One golden test file in `tests/unit/`, covering 6 truths, using monkeypatch to intercept `search_by_owner_name` calls so the test runs offline (no AssuranceWeb HTTP).
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
@src/probate_property_locator.py
@src/madison_property_api.py

<interfaces>
<!-- Key facts so the executor doesn't have to re-explore src/probate_property_locator.py -->

From src/probate_property_locator.py (lines 176-232 for `_search_madison`):
- Imports `madison_property_api.search_by_owner_name` at function scope (lazy import) — monkeypatching MUST target the locator module's reference path OR (cleaner) the `madison_property_api` module attribute. Recommended: `monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)`.
- Branches:
  1. Comma in name → split on first comma → one call to `search_by_owner_name(last, first)`.
  2. Empty parts → return [].
  3. Single token → one call to `search_by_owner_name(parts[0], None)`.
  4. Multi-token (default) → BOTH interpretations tried, results deduped by parcel_number.
- Dedup key: `getattr(rec, "parcel_number", "") or getattr(rec, "parcel_id", "")` — so the test fake records must expose at least one of those attributes.

From src/madison_property_api.py:
- `MadisonPropertyRecord` is `@dataclass(frozen=True)` with `parcel_number: str` field. The test fakes can be simple namedtuple/SimpleNamespace stand-ins with a `parcel_number` attribute — no need to instantiate the real frozen dataclass.

From src/probate_property_locator.py (lines 120-173 for `_search_jefferson`):
- Has THREE retry layers (original, last-first reorder, truncate-to-LAST+FIRST). The Jefferson guard test should assert that at least the last-first retry fires for "FIRST MIDDLE LAST" input — the truncate variants are nice-to-have but not the regression class BUGFIX-01 was about.

Shared test infrastructure already exists from plan 01-bugfix-02:
- `tests/unit/conftest.py` — adds `src/` to sys.path
- `tests/unit/__init__.py` — present
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Golden test for _search_madison + _search_jefferson name-format handling</name>
  <files>tests/unit/test_search_madison_name_format.py</files>

  <behavior>
    The test file must implement at least these 6 cases as separate pytest functions:

    1. `test_madison_space_separated_tries_both_interpretations` — Input `"MARY ANGELA SMITH"`. Monkeypatch `madison_property_api.search_by_owner_name` with a fake that records every `(last, first)` call. Assert the recorded calls contain BOTH `("SMITH", "MARY ANGELA")` AND `("MARY", "ANGELA SMITH")`, in either order. This is the primary BUGFIX-01 guard.

    2. `test_madison_comma_form_uses_single_call` — Input `"SMITH, MARY ANGELA"`. Assert exactly one call, with `last="SMITH"`, `first="MARY ANGELA"` (no last-first ambiguity to resolve).

    3. `test_madison_single_token_calls_once` — Input `"SMITH"`. Assert exactly one call, with `last="SMITH"`, `first=None`.

    4. `test_madison_empty_returns_empty_list` — Input `""`. Assert returns `[]` and (recommended) zero calls to `search_by_owner_name`.

    5. `test_madison_dedupes_by_parcel_number` — Fake returns the SAME record (same `parcel_number`) for both `(parts[-1], parts[:-1])` and `(parts[0], parts[1:])` queries. Assert the function returns ONE result, not two. Use a `SimpleNamespace(parcel_number="14-06-23-4-000-043.000", ...)` stub or equivalent.

    6. `test_jefferson_still_retries_last_first` — Parallel guard on the sibling. Input `"OPAL W SMITH"` (probate-notice format). Monkeypatch `jefferson_property_api.search_by_owner_name`. Assert that AT LEAST one of the recorded calls matches the last-first reorder pattern (i.e. starts with `"SMITH"` or includes `"SMITH OPAL"`). Do NOT assert exact call count — `_search_jefferson` has 3 retry layers and additional ones may be added; the regression class is "the reorder retry disappears", not "the retry count changes".

    All 6 tests must be offline — no real HTTP, no real AssuranceWeb / E-Ring calls. Use pytest's `monkeypatch` fixture for the fakes.
  </behavior>

  <action>
    Create `tests/unit/test_search_madison_name_format.py`.

    Module docstring: reference BUGFIX-01 (CONCERNS.md "Madison probate-property locator passes the wrong name") and explain that the test uses `monkeypatch.setattr("madison_property_api.search_by_owner_name", ...)` because the locator imports it lazily inside the function (so patching `probate_property_locator.search_by_owner_name` would not work — that name doesn't exist at module scope).

    Pattern (verified compatible with the existing fix per the inline reading above):
    ```python
    import types
    from typing import Any
    import pytest
    import probate_property_locator as locator

    def _make_rec(parcel: str) -> Any:
        # Lightweight stand-in for MadisonPropertyRecord — duck-typed on
        # parcel_number, which is the dedup key in _record_to_match.
        return types.SimpleNamespace(parcel_number=parcel)

    def test_madison_space_separated_tries_both_interpretations(monkeypatch):
        calls: list[tuple[str, str | None]] = []
        def fake(last, first=None, **kw):
            calls.append((last, first))
            return []
        monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)
        locator._search_madison("MARY ANGELA SMITH")
        assert ("SMITH", "MARY ANGELA") in calls, f"missing FIRST-MIDDLE-LAST interpretation; got {calls}"
        assert ("MARY", "ANGELA SMITH") in calls, f"missing LAST-FIRST-MIDDLE interpretation; got {calls}"
    ```

    Use the `# ── ... ──` box-drawing section banners (CONVENTIONS.md) to organize the file into Madison guards, Jefferson guard, and helpers.

    Per CONVENTIONS.md: `from __future__ import annotations` at top (so the `tuple[str, str | None]` syntax works on Python 3.10+); no `Optional[...]` — PEP 604 union syntax only; lazy `%`-style if logging is used (likely not needed here).

    Do NOT modify src/probate_property_locator.py, src/madison_property_api.py, or src/jefferson_property_api.py. The fix is already in place. If a test fails on first run against the current code, that is the signal that the fix has regressed — surface that to the user before "fixing" the test.

    Reuse the `tests/unit/conftest.py` from plan 01-bugfix-02 (sys.path bootstrap is already wired).
  </action>

  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack &amp;&amp; python -m pytest tests/unit/test_search_madison_name_format.py -v</automated>
  </verify>

  <done>
    All 6 tests pass against the current (fixed) probate_property_locator.py.

    To prove the guards work: temporarily revert `_search_madison` to its original buggy form (only the `(parts[0], parts[1:])` branch, no second interpretation) and rerun — at least `test_madison_space_separated_tries_both_interpretations` and `test_madison_dedupes_by_parcel_number` must fail. Restore the file before completing the task.

    Test file is offline (no network calls), uses `monkeypatch` to fake the county API, and lives in `tests/unit/`.
  </done>
</task>

</tasks>

<verification>
**Phase-level checks for this plan:**
- `python -m pytest tests/unit/test_search_madison_name_format.py -v` shows 6 PASSED.
- `grep -nE "search_by_owner_name\(parts\[-1\]" src/probate_property_locator.py` returns at least one match (confirms FIRST-MIDDLE-LAST interpretation still in place).
- `grep -nE "search_by_owner_name\(parts\[0\]" src/probate_property_locator.py` returns at least one match (confirms LAST-FIRST-MIDDLE interpretation still in place — both must coexist).
- No production source files modified (`git diff --stat src/` shows nothing).
</verification>

<success_criteria>
1. `tests/unit/test_search_madison_name_format.py` exists with 6 test functions.
2. All 6 tests pass against current src/.
3. Tests are offline (monkeypatched, no real HTTP).
4. Dedup test uses a record duck-typed on `parcel_number` (not a real MadisonPropertyRecord — that frozen dataclass takes too many required fields for a unit test stub).
5. The Jefferson guard (#6) asserts the regression class ("last-first retry is gone") without over-constraining the exact call count.
6. The bug class — "_search_madison only tries one interpretation" — is now caught automatically by pytest.
</success_criteria>

<output>
After completion, create `.planning/phases/01-stabilize-production/01-stabilize-production-01-bugfix-01-SUMMARY.md` documenting:
- Why monkeypatching at `"madison_property_api.search_by_owner_name"` (and not `probate_property_locator.search_by_owner_name`) is the correct target — the locator does lazy function-scope imports
- The 6 truths the test asserts and how each maps to the BUGFIX-01 bug class
- Any deviations from the plan
</output>
