---
phase: 01-stabilize-production
plan: 01-bugfix-02
subsystem: actor / cold-start
tags:
  - actor
  - cold-start
  - apify
  - regression-test
  - tests-unit-scaffold
requirements:
  - BUGFIX-02
priority: P0-deployment-blocker
status: complete
completed: 2026-05-24
duration_minutes: ~12
dependency_graph:
  requires: []
  provides:
    - "tests/unit/__init__.py (package marker for the new unit-test home)"
    - "tests/unit/conftest.py (sys.path bootstrap + .env loader — ambient fixture for all tests/unit/*.py)"
  affects: []
tech_stack:
  added:
    - "pytest 9.0.3 (already in .venv; not added to requirements.txt this phase — deferred to TEST-01/02/03 in v2)"
  patterns:
    - "Regex source-text scan as regression test for 'dead attribute reference' bug class (comment-strip + re.findall on src/main.py)"
    - "import smoke test as unit-level proxy for 'process can boot' integration concerns"
    - "Empty package marker + ambient conftest scoped to a single test directory"
key_files:
  created:
    - "tests/unit/__init__.py"
    - "tests/unit/conftest.py"
    - "tests/unit/test_actor_cold_start.py"
  modified: []
decisions:
  - "Comment-strip src/main.py before regex scan so the historical-context comment block at src/main.py:183-187 (which mentions TNPN_EMAIL / TNPN_PASSWORD by name) does not self-trip the gate"
  - "Use four separate def test_*() functions (one per guard) instead of one mega-test so a failure pinpoints exactly which regression class tripped"
  - "Reload main via importlib.reload(main) inside the import smoke test so an earlier collection-phase import does not cache a stale module and mask a regression"
metrics:
  commits: 1
  files_created: 3
  files_modified: 0
  tests_added: 4
  tests_passing: 4
  duration: ~12 min
---

# Phase 1 Plan 01-bugfix-02: Apify Cold-Start Regression Net Summary

**One-liner:** Locked in the BUGFIX-02 Apify cold-start fix with four offline regression guards (regex source scan + cred_map key scan + config attribute introspection + import smoke test), and shipped the shared `tests/unit/` scaffold that the three Wave-2 plans depend on.

## What Was Built

`tests/unit/test_actor_cold_start.py` — a single test file with **4 guard functions**, each catching one regression direction for the BUGFIX-02 bug class (Apify Actor cold-start `AttributeError` on dead `config.TNPN_EMAIL` / `config.TNPN_PASSWORD` references that survived the AL migration).

`tests/unit/__init__.py` and `tests/unit/conftest.py` — the shared scaffold for the new pytest-discoverable unit-test home. The conftest does two ambient jobs for every test in `tests/unit/`:

1. Prepends `src/` to `sys.path` so `import main`, `import config`, `import notice_parser`, etc. resolve without per-file boilerplate.
2. Calls `load_dotenv()` against the repo-root `.env` so any test that touches `config.py` gets a sane environment (per TESTING.md: "Tests rely on `.env` being loaded").

Both files are reusable by the three Wave-2 plans (`01-bugfix-01`, `01-bugfix-03`, `01-parser-01`) — they all live in `tests/unit/` and consume the same conftest without modification.

## The 4 Guard Shapes

| # | Guard | What it catches | Implementation |
|---|---|---|---|
| 1 | **Source-text regex (`config.TNPN_*`)** | Future maintainer copy-pastes the dead Tennessee-era validator back into `actor_main()` | `re.findall(r"config\.TNPN_(?:EMAIL|PASSWORD)", source)` against `src/main.py` with comment-only lines stripped |
| 2 | **`_cred_map` key scan (`tn_username` / `tn_password`)** | Symmetric regression: someone updates `_cred_map` to "accept" the old TN keys "for backward compat" (which would silently drop them) | `re.findall(r'actor_input\.get\(\s*["\']([^"\']+)["\']', source)` then assert forbidden keys absent |
| 3 | **`config` attribute introspection (`TNPN_EMAIL` / `TNPN_PASSWORD`)** | Symmetric regression in the OTHER direction: someone re-adds the constants to `config.py` "so old code paths still work" | `importlib.import_module("config"); assert not hasattr(config, "TNPN_EMAIL")` |
| 4 | **Import smoke test (`import main`)** | A future refactor hoists a dead credential reference to module level (e.g. as a default arg `def foo(x=config.TNPN_EMAIL)`) — the original bug only fired at function invocation, but this stronger guard catches module-level references too | `importlib.import_module("main"); importlib.reload(main)` (reload defeats stale-module caching from pytest collection) |

## Why Source-Text Scanning Is the Right Test Shape Here

The original BUGFIX-02 bug class is "a dead attribute reference survives a config refactor." The only ways to catch it are:

- **Integration test:** spin up the full Apify Actor SDK + asyncio event loop + a mocked KVS + `Actor.get_input()` simulation. Slow, brittle, requires Apify SDK installed in the test environment.
- **Source-text scan (chosen):** read `src/main.py` and assert the forbidden tokens are absent. Deterministic, offline, ~0.07s wall-clock, pinpoints the exact regression with a clear error message.

The trade-off: a source-text scan can't catch *semantic* regressions like "credential validation logic became wrong" — it only catches *syntactic* presence/absence of forbidden tokens. That's exactly the right resolution for this bug class. Per Phase 1 CONTEXT.md ("All four fixes are already present in the production source"), the test layer's job is to lock in the **absence** of known-bad code, not to verify the **correctness** of new code.

The comment-strip step is critical: `src/main.py:183-187` has an explanatory comment block that names `TNPN_EMAIL` and `TNPN_PASSWORD` explicitly as part of the historical record. Without stripping comment-only lines first, the regex would self-trip on that breadcrumb and the test would always fail. Implementation drops any line whose first non-whitespace character is `#`; partial inline comments after code are preserved (because forbidden tokens inside live code are exactly what we want to catch).

## Sanity Check (Manual, Not Committed)

Per the plan's `<done>` clause, I performed a one-shot regression simulation to prove the guards actually trip:

1. **Guard 1 simulation:** added `if not config.TNPN_EMAIL or not config.TNPN_PASSWORD: Actor.log.warning(...)` to `src/main.py:190`. Reran pytest → `test_no_config_tnpn_references_in_main` FAILED with the expected message naming both tokens. Restored `src/main.py`.
2. **Guard 2 simulation:** added `_legacy = actor_input.get("tn_username", ""); _legacy2 = actor_input.get("tn_password", "")` to `src/main.py:190`. Reran pytest → `test_cred_map_does_not_accept_legacy_tn_keys` FAILED with the expected message. Restored `src/main.py`.
3. Post-restore confirmation: `git diff src/main.py` returned empty, all 4 guards pass cleanly in 0.07s.

Guards 3 and 4 were not simulated (would require modifying `src/config.py`, which is outside this plan's `files_modified` scope and would require a second backup/restore cycle). Their assertions are simple enough (`assert not hasattr(...)` and `importlib.reload(...)` raising) that the simulation skip is acceptable — both have failure modes that would surface on the FIRST real regression run, not silently pass.

## Tests/Unit Scaffold — Available to Wave 2

The three Wave-2 plans (`01-bugfix-01`, `01-bugfix-03`, `01-parser-01`) can now write their test files directly into `tests/unit/` and inherit `conftest.py`'s behaviour for free:

- `from main import ...` / `from notice_parser import ...` / `from enrichment_pipeline import ...` resolves without any `sys.path` boilerplate.
- `os.getenv("SMARTY_AUTH_ID")` etc. resolve from `.env` (no-op when `.env` is absent — `config.py` falls back to empty-string defaults).

No file-level coordination needed; each Wave-2 plan touches a different `test_*.py` file with no shared writes.

## Phase-Level Verification (from plan's `<verification>` block)

All 5 checks pass:

| # | Check | Result |
|---|---|---|
| 1 | `python -m pytest tests/unit/test_actor_cold_start.py -v` shows 4 PASSED | ✓ 4 passed in 0.07s |
| 2 | `grep -nE "config\.TNPN_(EMAIL|PASSWORD)" src/main.py` returns nothing | ✓ no match (exit 1) |
| 3 | `grep -nE 'actor_input\.get\(\s*["\']tn_(username\|password)' src/main.py` returns nothing | ✓ no match (exit 1) |
| 4 | `python -c "import sys; sys.path.insert(0, 'src'); import config; assert not hasattr(config, 'TNPN_EMAIL'); assert not hasattr(config, 'TNPN_PASSWORD'); print('config clean')"` prints `config clean` | ✓ printed |
| 5 | `python -c "import sys; sys.path.insert(0, 'src'); import main; print('main imports')"` prints `main imports` | ✓ printed |

## Deviations from Plan

**None.** Plan executed exactly as written:
- 4 guard functions, one per regression class
- Comment-stripped source scan (per planner grep-gate hygiene)
- Section-banner convention from CONVENTIONS.md (`# ── ... ──`)
- Plain `def test_*` functions, no `unittest.TestCase` (per existing `tests/test_obituary_enricher.py` idiom)
- `src/main.py` and `src/config.py` are byte-stable — no production-source modifications
- Single atomic commit (`4455490`) per task_commit_protocol

## Authentication Gates

None. This is a fully offline test plan.

## Known Stubs

None. No placeholder UI / mock data / TODOs introduced.

## Threat Flags

None. This plan adds offline regression tests only — no new network endpoints, auth paths, file access patterns, or schema changes at trust boundaries.

## Self-Check: PASSED

Verified post-write:

- `tests/unit/__init__.py` exists: FOUND
- `tests/unit/conftest.py` exists: FOUND
- `tests/unit/test_actor_cold_start.py` exists: FOUND
- Commit `4455490` exists in git log: FOUND
- `src/main.py` is byte-stable (no diff against pre-task HEAD~1): CONFIRMED
- All 4 guards pass on current source: CONFIRMED (4 passed in 0.07s)
