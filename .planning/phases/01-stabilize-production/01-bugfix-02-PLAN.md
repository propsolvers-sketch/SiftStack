---
phase: 01-stabilize-production
plan: 01-bugfix-02
type: execute
wave: 1
depends_on: []
files_modified:
  - tests/unit/__init__.py
  - tests/unit/conftest.py
  - tests/unit/test_actor_cold_start.py
  - src/main.py
autonomous: true
requirements:
  - BUGFIX-02
priority: P0-deployment-blocker
tags:
  - actor
  - cold-start
  - apify
  - regression-test
must_haves:
  truths:
    - "Importing src/main.py succeeds without AttributeError on a fresh interpreter (no module-level reference to the deleted config.TNPN_EMAIL / config.TNPN_PASSWORD constants)"
    - "actor_main() does not reference config.TNPN_EMAIL or config.TNPN_PASSWORD anywhere in its body"
    - "The Actor _cred_map dict does not advertise tn_username or tn_password as accepted Actor-input keys (those keys would silently be discarded by the loop that writes os.environ)"
    - "An automated test simulates the Actor cold-start AttributeError condition (config without TNPN_* attrs) and proves main.actor_main can be invoked far enough to fail on input rather than on attribute access"
  artifacts:
    - path: "tests/unit/__init__.py"
      provides: "Marks tests/unit as a package so pytest can discover it"
    - path: "tests/unit/conftest.py"
      provides: "Shared sys.path bootstrap so tests/unit/*.py can import src/* modules without per-file boilerplate"
    - path: "tests/unit/test_actor_cold_start.py"
      provides: "Golden test that fails if main.py ever re-introduces a config.TNPN_EMAIL / TNPN_PASSWORD reference, or if _cred_map silently grows a tn_username / tn_password key"
      min_lines: 30
  key_links:
    - from: "tests/unit/test_actor_cold_start.py"
      to: "src/main.py"
      via: "import + AST/source scan + Actor input simulation"
      pattern: "TNPN_EMAIL|TNPN_PASSWORD|tn_username|tn_password"
---

<objective>
Lock in the BUGFIX-02 fix (Apify Actor cold-start `AttributeError` on dead `config.TNPN_EMAIL` / `config.TNPN_PASSWORD` references at `src/main.py:184`) with a regression test net that:

1. **Verifies the fix is still in place** — the dead `config.TNPN_*` references and the `tn_username` / `tn_password` Actor-input keys are not silently reintroduced by a future merge.
2. **Establishes `tests/unit/` as the canonical home** for the four Phase-1 regression tests (this plan ships the scaffolding the other 3 plans depend on — `__init__.py` + `conftest.py`).
3. **Confirms `actor_main()` imports cleanly** — the original bug was that the function body referenced an attribute deleted from `config.py` in the same diff, so any test that triggers Actor.get_input() would crash at attribute access *before* anything productive runs.

Purpose: This is the deployment-blocker in the phase. Until a test asserts the absence of the dead references, every Apify schedule on the current code is dead-on-arrival and there is no automated guardrail against the bug class returning when future credential refactors happen.

Output: A `tests/unit/` scaffold (importable from pytest), a single golden test file covering BUGFIX-02, and confirmation that the fix in `src/main.py` is byte-stable.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/STATE.md
@.planning/REQUIREMENTS.md
@.planning/codebase/CONCERNS.md
@.planning/codebase/CONVENTIONS.md
@.planning/codebase/TESTING.md

# Relevant source — the fixed cold-start path
@src/main.py
@src/config.py

<interfaces>
<!-- Key facts the executor needs about the current (post-fix) Actor cold-start path -->
<!-- so they don't have to spelunk to confirm the bug is actually gone. -->

From src/main.py (the fix, lines ~140-200 of actor_main()):
- `_cred_map` at lines ~147-160 maps ONLY these Actor-input keys (NO tn_username / tn_password):
    CAPTCHA_API_KEY, ANTHROPIC_API_KEY, SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN,
    OPENWEBNINJA_API_KEY, SERPER_API_KEY, FIRECRAWL_API_KEY, TRACERFY_API_KEY,
    DATASIFT_EMAIL, DATASIFT_PASSWORD, SLACK_WEBHOOK_URL, TRESTLE_API_KEY
- The validator at line ~188 is now `if not config.CAPTCHA_API_KEY: Actor.log.warning(...)` — no TNPN_* attribute access. Comment block at lines 183-187 explicitly documents that the legacy TNPN_EMAIL / TNPN_PASSWORD validator used to crash on cold-start with AttributeError.
- `src/config.py` no longer defines TNPN_EMAIL or TNPN_PASSWORD at module level (replaced by APNA_EMAIL / APNA_PASSWORD, which the AL site does not actually need since alabamapublicnotices.com is loginless).

From src/config.py:
- `getattr(config, "TNPN_EMAIL", _SENTINEL)` returns `_SENTINEL`, confirming the deletion.
- The module is safe to `import config` from a unit test (it `load_dotenv()`s but does not require any specific env var to be present).

The fix is byte-stable as of 2026-05-23. This plan's job is to prevent regression, not to re-apply the fix.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Scaffold tests/unit/ + golden cold-start regression test for BUGFIX-02</name>
  <files>tests/unit/__init__.py, tests/unit/conftest.py, tests/unit/test_actor_cold_start.py</files>

  <behavior>
    The test file must, at minimum, fail loudly if any of these regressions occur:

    1. **Source-text guard (regex over src/main.py source)**: assert that `re.search(r"config\.TNPN_EMAIL|config\.TNPN_PASSWORD", main_source)` returns None. This catches a future maintainer copy-pasting the dead validator back. Use a comment-stripped scan so a `# TNPN_EMAIL` history note in a docstring doesn't self-invalidate the gate (per planner grep-gate hygiene). Implementation: read `src/main.py`, drop lines whose first non-whitespace character is `#`, then run the regex on the joined remainder.

    2. **`_cred_map` key guard (runtime introspection or static parse)**: assert that the strings `"tn_username"` and `"tn_password"` do NOT appear as `actor_input.get(...)` keys anywhere in `src/main.py`. This catches the symmetric regression where someone updates `_cred_map` to "accept" the old TN keys "for backward compat".

       Implementation hint: `re.findall(r"""actor_input\.get\(\s*["']([^"']+)["']""", main_source)` → assert `"tn_username" not in keys and "tn_password" not in keys`.

    3. **config.py attribute guard**: `import config; assert not hasattr(config, "TNPN_EMAIL") and not hasattr(config, "TNPN_PASSWORD")`. Catches the symmetric "let's re-add the constants so old code paths still work" regression. Use the project sys.path bootstrap from conftest.py.

    4. **Actor-input simulation (the cold-start trigger)**: import `main` from a fresh interpreter perspective and verify the module imports without raising. Do NOT actually invoke `actor_main()` (that would require Apify SDK + an event loop and is integration-level). The plain `import main` is the unit-level proxy for "the file is loadable" — which was the entire failure mode the original BUGFIX-02 produced *before* the scrape ever started.

       Implementation: `importlib.import_module("main")` inside the test; if config.TNPN_EMAIL had been referenced at module level (not just inside actor_main), this would crash. The current fix has the reference inside the function body, so this passes; if a regression hoists the reference to module level (e.g. as a default arg), the test fails.

    All four assertions go in one test file. Use four separate `def test_*()` functions so a single failure pinpoints which guard tripped.
  </behavior>

  <action>
    Create `tests/unit/__init__.py` as an empty marker file (presence-only — pytest does not require it but it makes the package importable for any future shared helpers, mirroring the existing `tests/` layout where modules are siblings, not packages).

    Create `tests/unit/conftest.py` with the project's canonical `sys.path` bootstrap so `from main import ...` works from inside `tests/unit/`. Follow the existing pattern at `tests/test_parser.py:6-8` (`sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))`). Also add `load_dotenv()` from python-dotenv so any test that touches `config` gets a sane env (per TESTING.md "Tests rely on `.env` being loaded").

    Create `tests/unit/test_actor_cold_start.py` implementing the four guards described in <behavior>. Module docstring should reference BUGFIX-02 (CONCERNS.md "Apify Actor cold-start `AttributeError`") and explain WHY the source-text scan is the appropriate test shape here — the bug class is "a dead attribute reference survives a config refactor", and the only way to catch that without spinning up the Apify Actor SDK is to scan the source for the forbidden strings.

    Use the section-banner convention from CONVENTIONS.md (`# ── ... ──`) to organize the four guards. Use `logger.warning` / `print` only via the pytest assertion path; no module-level logging. Follow the existing offline-test idiom from `tests/test_obituary_enricher.py` (plain `def test_*` functions, no `unittest.TestCase`).

    Do NOT modify `src/main.py` — the fix is already in place. If the test fails on first run, that is the signal that the fix has regressed and the user should be alerted, not that the test is wrong.

    Per CONVENTIONS.md: empty strings (never None), lazy `%`-style logger formatting if any logging is used, `from __future__ import annotations` not needed here (no dataclasses), keyword-only args not applicable (no API surface).
  </action>

  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack &amp;&amp; python -m pytest tests/unit/test_actor_cold_start.py -v</automated>
  </verify>

  <done>
    All four guard tests pass on first run against the current (fixed) src/main.py + src/config.py.

    To prove the guards work: temporarily reintroduce `config.TNPN_EMAIL` as a module-level reference in src/main.py (or add `"tn_username": actor_input.get("tn_username", "")` to _cred_map) and rerun the test — it must fail. Restore src/main.py before completing the task. This step is performed manually by the executor as a one-shot sanity check; it is not committed.

    `tests/unit/__init__.py` exists (empty).
    `tests/unit/conftest.py` adds src/ to sys.path and load_dotenv()s.
    `tests/unit/test_actor_cold_start.py` has 4 test functions and a module docstring referencing BUGFIX-02 + CONCERNS.md.
  </done>
</task>

</tasks>

<verification>
**Phase-level checks for this plan:**
- `python -m pytest tests/unit/test_actor_cold_start.py -v` shows 4 PASSED.
- `grep -nE "config\.TNPN_(EMAIL|PASSWORD)" src/main.py` returns nothing (confirms fix still in place).
- `grep -nE 'actor_input\.get\(\s*["'"'"']tn_(username|password)' src/main.py` returns nothing.
- `python -c "import sys; sys.path.insert(0, 'src'); import config; assert not hasattr(config, 'TNPN_EMAIL'); assert not hasattr(config, 'TNPN_PASSWORD'); print('config clean')"` prints `config clean`.
- `python -c "import sys; sys.path.insert(0, 'src'); import main; print('main imports')"` prints `main imports` (no AttributeError on import).
</verification>

<success_criteria>
1. `tests/unit/` directory exists with `__init__.py` + `conftest.py` + `test_actor_cold_start.py`.
2. All 4 guards in `test_actor_cold_start.py` pass (regex source scan + cred_map key scan + config attribute introspection + import smoke test).
3. The 4 guards collectively assert: (a) main.py has no `config.TNPN_*` references, (b) main.py does not advertise `tn_username` / `tn_password` as Actor-input keys, (c) config has no `TNPN_*` attributes, (d) `import main` succeeds without AttributeError.
4. No production source code (src/) is modified — this plan is test-only.
5. Phase-1 test scaffolding (`tests/unit/__init__.py` + `conftest.py`) is now available for the other 3 BUGFIX/PARSER plans to share.
</success_criteria>

<output>
After completion, create `.planning/phases/01-stabilize-production/01-stabilize-production-01-bugfix-02-SUMMARY.md` documenting:
- The 4 guard shapes (source-text regex, cred_map key scan, config attribute introspection, import smoke)
- Why source-text scanning is the appropriate test shape for "dead attribute reference" bug classes
- That `tests/unit/__init__.py` + `tests/unit/conftest.py` are now available for the BUGFIX-01 / BUGFIX-03 / PARSER-01 plans to reuse
- Any deviations from the plan
</output>
