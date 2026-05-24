# Phase 1: Stabilize Production — Planning Context

**Phase:** 01-stabilize-production
**Milestone:** v1.1 — Stabilize + Schedule + Close Coverage Gaps
**Goal (per ROADMAP):** Eliminate the 3 silent bugs + 1 parser gap that are degrading lead quality and blocking Apify daily runs. After this phase, production runs are trustworthy.
**Requirements in scope:** BUGFIX-01, BUGFIX-02, BUGFIX-03, PARSER-01
**Plan count:** 4 (one per requirement)
**Status:** Plans drafted 2026-05-23

---

## Key Finding from Source Audit

**All four fixes are already present in the production source code** as of the planning baseline:

| Req | Code location | Status |
|-----|---------------|--------|
| BUGFIX-01 | `src/probate_property_locator.py:176-232` (`_search_madison` tries BOTH interpretations + dedup) | ✓ Fixed |
| BUGFIX-02 | `src/main.py:140-200` (no `config.TNPN_*` refs; `_cred_map` does not advertise `tn_username`/`tn_password`) | ✓ Fixed |
| BUGFIX-03 | `src/enrichment_pipeline.py:235` (`_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")`) | ✓ Fixed |
| PARSER-01 | `src/notice_parser.py:635` (`PR_ADDRESS_NAME_FIRST_RE`) + `:1982` (OR chain in `_parse_pr_address`) | ✓ Fixed |

This means **Phase 1's actual deliverable is the regression test net** that locks the fixes in. Per ROADMAP Success Criterion #5: *"A single `pytest tests/unit/` run covers all 4 fixes with golden tests that would have caught the original bug class."*

No production source modifications are planned. Any test that fails on first run against current `src/` is a signal that the fix has regressed — surface to the user before "fixing" the test.

---

## 4-Plan Structure

| Wave | Plan | Requirement | Files Modified | Priority |
|------|------|-------------|----------------|----------|
| 1 | `01-bugfix-02.md` | BUGFIX-02 (Apify cold-start) | `tests/unit/__init__.py` + `conftest.py` + `test_actor_cold_start.py`, src/main.py (read-only verify) | **P0 — deployment blocker** |
| 2 | `01-bugfix-01.md` | BUGFIX-01 (Madison name-format) | `tests/unit/test_search_madison_name_format.py` | P1 — silent quality regression |
| 2 | `01-bugfix-03.md` | BUGFIX-03 (`_GARBAGE_RE` mismatch) | `tests/unit/test_garbage_address_validator.py` | P1 — silent quality regression |
| 2 | `01-parser-01.md` | PARSER-01 (AL signature-block PR address) | `tests/unit/test_pr_address_name_first.py` | P1 — silent quality regression |

### Wave structure (parallel execution)

- **Wave 1 — BUGFIX-02 alone.** It ships the shared `tests/unit/` scaffold (`__init__.py` + `conftest.py`). Waves 2 depend on those files existing.
- **Wave 2 — BUGFIX-01, BUGFIX-03, PARSER-01 in parallel.** Each touches a different test file with no shared writes; safe to run in any order after Wave 1 lands.

### Recommended execution order

1. `/gsd-execute-phase 01-stabilize-production` (or invoke 01-bugfix-02 alone first if you want to verify scaffolding before parallelizing).
2. After Wave 1 lands, the three Wave-2 plans can be executed in parallel — they have no file overlap and no logical dependencies between them.
3. After all 4 plans land: `cd /Users/shanismith/Desktop/SiftStack && python -m pytest tests/unit/ -v` should show all tests passing across all 4 files.

---

## Cross-plan touchpoints

| Touchpoint | Owner plan | Consumers |
|------------|------------|-----------|
| `tests/unit/__init__.py` | 01-bugfix-02 | All other plans (no write; package marker) |
| `tests/unit/conftest.py` (sys.path bootstrap + `load_dotenv`) | 01-bugfix-02 | All other plans (no write; ambient pytest fixture) |
| `src/main.py` source-text scan | 01-bugfix-02 | None — confined to BUGFIX-02 |
| `src/probate_property_locator.py` monkeypatch target | 01-bugfix-01 | None |
| `src/enrichment_pipeline.py` `_GARBAGE_RE` + `_validate_records` import | 01-bugfix-03 | None |
| `src/notice_parser.py` `PR_ADDRESS_NAME_FIRST_RE` + `_parse_pr_address` import | 01-parser-01 | None |

**No file-modification overlap between the 4 plans** — Wave-2 parallelism is safe.

---

## Test infrastructure decisions

- **New `tests/unit/` directory** (does not exist in repo today). All 4 plans deposit tests here. Establishes the canonical home for offline, fast, pytest-discoverable unit tests — a deviation from the existing pattern (mixed `tests/test_*.py` + repo-root `test_*.py` integration scripts described in TESTING.md). This is intentional per ROADMAP Success Criterion #5 wording: "a single `pytest tests/unit/` run covers all 4 fixes."
- **pytest as the runner.** Existing `tests/test_*.py` files use plain `assert` + `if __name__ == "__main__":` discovery loops; the new `tests/unit/` files use pytest's auto-discovery (`def test_*` at module scope, monkeypatch fixture). Pytest is not in `requirements.txt` today; each plan's `<automated>` verify line assumes pytest is invokable via `python -m pytest`. **If pytest is missing**, the first executor should `pip install pytest` in the project venv as a one-shot setup — but this is a pre-existing environment concern, not part of the plan scope.
- **All tests offline.** No real HTTP, no real AssuranceWeb / E-Ring / Apify calls. Uses `monkeypatch` (BUGFIX-01) or pure-function fixtures (BUGFIX-03, PARSER-01) or static source scans (BUGFIX-02).
- **No production source modifications.** Per the audit above, all 4 fixes are already in place. If any test fails on first run, that indicates a regression — surface it to the user, don't "fix" the test.

---

## Risk register

| Risk | Mitigation |
|------|------------|
| pytest not installed in project venv | First executor adds pytest to local venv (`pip install pytest`) as one-shot setup; not committed to requirements.txt this phase. v2 may add to requirements as part of the deferred TEST-01/02/03 reorg. |
| Fix has silently regressed since planning baseline (2026-05-23) | Tests fail on first run; executor MUST surface to user as a regression alert, not "fix" the test. Source-text scans in 01-bugfix-02 are specifically designed to catch this class. |
| `tests/unit/conftest.py` sys.path order conflicts with repo-root `test_*.py` scripts | Conftest is scoped to `tests/unit/` only — does not affect repo-root or `tests/` siblings. No collision risk. |
| Future maintainer "consolidates" the 4 county adapters (REFAC-01) and silently drops _search_madison / _search_jefferson retry logic | Plan 01-bugfix-01 explicitly tests BOTH siblings to prevent this. |
| Phase 1 testing patterns drift from existing repo convention | Documented as intentional in this CONTEXT.md. The existing pattern (function-discovery + bare assert) is preserved for the legacy `tests/test_*.py` files; pytest-style is the NEW pattern for `tests/unit/`. Future plans should follow the `tests/unit/` convention for offline regression tests. |

---

## Phase exit criteria

After all 4 plans execute successfully:

1. `tests/unit/` exists with at least 4 test files (`test_actor_cold_start.py`, `test_search_madison_name_format.py`, `test_garbage_address_validator.py`, `test_pr_address_name_first.py`) plus `__init__.py` + `conftest.py`.
2. `python -m pytest tests/unit/ -v` shows all tests passing (test #10 in BUGFIX-03 may skip; that's acceptable per its plan).
3. Each of the 4 fixes is now pinned by at least one automated assertion that would have caught the original bug class.
4. No production source under `src/` has been modified.
5. STATE.md updated: phase 1 complete; ready to plan phase 2 (Funnel Transparency).

---

## What's explicitly NOT in scope for Phase 1

- Owner-street garbage validation (test #10 in BUGFIX-03 may skip if the validator doesn't currently check it — that's a follow-up ticket, not a Phase 1 deliverable).
- Pytest added to `requirements.txt` (deferred — environment concern, paired with the v2 TEST-01/02/03 reorg).
- Refactoring the 4 county adapters into a `CountyPropertyAdapter` Protocol (REFAC-01 — v2).
- Test coverage for the rest of the codebase (the 50+ untested modules per TESTING.md "What Is NOT Tested" — out of scope; this phase is targeted at the 4 known bug classes only).
- Any change to existing `tests/test_*.py` or repo-root `test_*.py` files (their function-discovery + bare-assert pattern is preserved).

---

*Planning context generated 2026-05-23 by gsd-planner.*
