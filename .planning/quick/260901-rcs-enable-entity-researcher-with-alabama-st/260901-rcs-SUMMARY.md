---
phase: quick-260901-rcs
plan: 01
subsystem: enrichment
tags: [entity-research, state-resolution, alabama, workflow, tdd]
requires:
  - src/state_resolver.py (state_full_name, state_for_county)
  - src/notice_parser.py (NoticeData)
provides:
  - "_resolve_search_state(notice) — full-state-name resolver for entity web search"
  - "nightly Step 3a Entity Research execution (--research-entities on main.py daily)"
affects:
  - src/enrichment_pipeline.py (Step 3a now runs in production)
  - .github/workflows/daily-sweep.yml (main.py daily step)
tech-stack:
  added: []
  patterns:
    - "state_resolver as the single source of truth for state expansion (no new abbrev maps)"
    - "length guard before state_full_name() to pass already-expanded names through"
key-files:
  created: []
  modified:
    - src/entity_researcher.py
    - tests/test_entity_researcher.py
    - .github/workflows/daily-sweep.yml
decisions:
  - "Guard on len(value) > 2 rather than importing state_resolver._normalize_state_name (private)"
  - "_search_entity default changed to empty string, resolved in-body to DEFAULT_PROPERTY_STATE's full name"
  - "Flag added only to main.py daily — trustee adapters do not accept --research-entities"
metrics:
  duration: ~7 min
  tasks: 2
  files: 3
  completed: 2026-09-01
---

# Phase quick-260901-rcs Plan 01: Enable Entity Researcher with Alabama State Wiring Summary

Turned on the already-built Entity Researcher in the nightly sweep and replaced its hardcoded Tennessee-era state literal with a `state_resolver`-backed resolver, so Alabama entity searches now carry the token "Alabama" instead of the ambiguous bare "AL".

## What Was Built

### Task 1 — `_resolve_search_state()` + resolver-backed state wiring

`src/entity_researcher.py` (commit `18e5e61`):

- Added `from state_resolver import state_for_county, state_full_name` to the flat import block. `state_resolver` is pure stdlib, so no circular-import or dependency risk.
- Added module-level `_resolve_search_state(notice: NoticeData) -> str` immediately above `_search_entity` in the Web Search section. Resolution order: strip `notice.state` → if empty, derive an abbreviation via `state_for_county(notice.county)` → if the value is longer than 2 characters, return it unchanged (already-expanded name) → otherwise `state_full_name(value)`.
- Rewired both call sites. `_research_single_entity` lost the three-line `state = notice.state or "Tennessee"` / `if state == "TN"` block and now passes `_resolve_search_state(notice)` straight into `_search_entity`. `_search_entity`'s signature default moved from `"Tennessee"` to `""`, and the token is resolved in-body as the stripped argument or `state_full_name(None)` (which yields the `DEFAULT_PROPERTY_STATE` full name, "Alabama").
- The query f-string shape is byte-identical apart from the substituted token.

No new abbreviation-to-name mapping was introduced anywhere — `state_resolver` owns that mapping exclusively.

**The landmine guard is the point of the task.** `state_full_name()` looks its argument up in `_STATE_LITERALS`, which is keyed by 2-letter abbreviation only. A full name like `"Tennessee"` misses the key, hits the unknown-abbrev branch, and silently returns `"Alabama"`. The `len(value) > 2` check short-circuits before that call, so a TN record whose state field already holds a full name cannot be corrupted into an Alabama search. The plan explicitly ruled out importing `state_resolver._normalize_state_name` (private), so the length check is the sanctioned guard.

### Task 2 — `--research-entities` on the nightly workflow

`.github/workflows/daily-sweep.yml` (commit `9baf025`):

- Added `--research-entities \` to the `python src/main.py daily` invocation inside the step named "Run main.py daily (foreclosure + probate)", inserted between `--tiers 1,2 \` and `--no-raw-csv \`, preserving the surrounding indentation and backslash continuation style.
- The flag was already registered on the top-level argparse parser at `src/main.py:1348`, so argparse accepts it unchanged. It is consumed as `skip_entity_research=not getattr(args, "research_entities", False)`, which flips Step 3a on.
- `ANTHROPIC_API_KEY` is already a job-level env var (line 82), so no secret wiring was required.
- No other pipeline step was touched. The trustee adapters (Rubin Lublin, T&B pending, T&B Results, Halliday Watkins Mann), pre-probate, AdHunter, and code-violation steps do not accept the argument, and `main.py daily` is the only path that reaches `enrichment_pipeline` Step 3a.

## Test Results

**55 passed, 0 failed, exit 0** — the 47-check baseline plus 8 new checks. The file remains the hand-rolled `check(name, actual, expected)` runner; it was not converted to pytest.

New checks under a `=== Search State Resolution ===` header, placed after "Name Parsing (Free Fast Path)" and before "Entity Filter Exemption":

| Input | Expected | Purpose |
|---|---|---|
| `NoticeData(state="AL", county="Jefferson")` | `"Alabama"` | **the actual bug fix — confirmed passing** |
| `NoticeData(state="TN", county="Knox")` | `"Tennessee"` | **regression guard — confirmed passing** |
| `NoticeData(state="", county="Knox")` | `"Tennessee"` | county fallback replaces the blind TN default |
| `NoticeData(state="", county="Jefferson")` | `"Alabama"` | county fallback, AL side |
| `NoticeData(state="", county="")` | `"Alabama"` | `DEFAULT_PROPERTY_STATE` |
| `NoticeData(state=" al ", county="")` | `"Alabama"` | messy casing / whitespace |
| `NoticeData(state="Tennessee", county="Knox")` | `"Tennessee"` | landmine guard |
| `inspect.signature(_search_entity).parameters["state"].default` | `""` | proves the TN literal default is gone |

Both the AL→"Alabama" and TN→"Tennessee" checks pass, confirming this is a generalization rather than an AL-only swap.

Supporting verifications:
- `"Tennessee"` string-literal count in non-comment lines of `src/entity_researcher.py`: **0**
- `grep -c 'from state_resolver import' src/entity_researcher.py`: **1**
- `grep -c '_resolve_search_state(notice)' src/entity_researcher.py`: **1**
- `yaml.safe_load()` on the workflow: **OK, jobs: ['daily-sweep']**
- `grep -c -- '--research-entities'` whole file: **1**; within the main.py daily step: **1**

No live DuckDuckGo search and no Anthropic API call was made during verification — every check exercises pure functions or greps text.

## TDD Gate Compliance

Plan task 1 carried `tdd="true"`. Gate sequence verified in git log:

1. **RED** — `4a0b7f5 test(260901-rcs)`: the 8 checks were added first and the suite failed with `ImportError: cannot import name '_resolve_search_state' from 'entity_researcher'`, exit 1.
2. **GREEN** — `18e5e61 feat(260901-rcs)`: implementation added, suite returned 55 passed / 0 failed, exit 0.
3. **REFACTOR** — not needed; no cleanup commit made.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Python interpreter path in the isolated worktree**

- **Found during:** Task 1 verification setup
- **Issue:** The plan's verify commands `cd` to `/Users/shanismith/Desktop/SiftStack` and use `.venv/bin/python`. This executor runs isolated in the git worktree at `.claude/worktrees/agent-a1c2a6af577f00945`, which has no `.venv` (it is gitignored and not materialized in linked worktrees). Running the plan's command verbatim would have tested the **main repo's** source, not the worktree's edits.
- **Fix:** Invoked the main repo's interpreter binary (`/Users/shanismith/Desktop/SiftStack/.venv/bin/python`) against the **worktree's** test path, run from the worktree root. The test script self-inserts `os.path.dirname(__file__)/../src` onto `sys.path`, so it resolves to the worktree's `src/` — confirmed by the RED-phase traceback, which names `.../agent-a1c2a6af577f00945/tests/../src/entity_researcher.py`.
- **Files modified:** none (tooling-only)
- **Commit:** n/a

**Note on the documented cold-import cost:** the plan warned that `import anthropic` blocks for minutes on a cold module cache (a verified 7m32s baseline) and to allow a 600000ms timeout. Both runs were given that timeout, but the cache was warm in this session — RED and GREEN each returned in seconds. No action needed; the guidance remains correct for a cold environment.

### Architectural Changes

None. No Rule 4 checkpoints were hit.

## Authentication Gates

None. `ANTHROPIC_API_KEY` was already wired at job level in the workflow, and no verification step made a live API call.

## Scope Discipline

Untouched, as the plan required: entity classification regexes, `ENTITY_SYSTEM_PROMPT` / `ENTITY_EXTRACT_PROMPT`, `_try_parse_entity_name` (the free name-parse fast path), the per-run `search_cache`, the `ThreadPoolExecutor` threading model, and `enrich_entity_data`. No package installs. No `.gitignore`, schedule, env, or secret edits. No changes to the `--skip-trace` flags on the trustee steps.

## Known Stubs

None. No hardcoded empty values, placeholder text, or unwired data sources were introduced.

## Operational Follow-Up (not scope)

Enabling this adds cost to the nightly run: one Claude Haiku call (`MAX_TOKENS = 256`) plus a DuckDuckGo search per entity candidate that misses the free name-parse fast path, at 4 parallel workers with a 0.5–1.0s inter-search delay. Entity candidates are a small slice of daily volume, so expect cents/day.

**Check the first post-merge nightly sweep** for the log line:

```
── Step 3a: Entity Research (N candidates) ──
```

followed by its counters:

```
  Name-parsed: N entities (free, no API calls)
  Web search: N/M entities found
```

Their presence confirms the feature is live for the first time; the `N/M` split sizes the actual per-run cost. If `Web search: skipped (no API key)` appears instead, `ANTHROPIC_API_KEY` is not reaching the step despite the job-level declaration. The step is `continue-on-error: true`, so a DDG rate-limit cannot fail the sweep.

## Self-Check: PASSED

- `src/entity_researcher.py` — FOUND
- `tests/test_entity_researcher.py` — FOUND
- `.github/workflows/daily-sweep.yml` — FOUND
- Commit `4a0b7f5` — FOUND
- Commit `18e5e61` — FOUND
- Commit `9baf025` — FOUND
- Deletion check across `bd0f659..HEAD` — clean, zero files deleted
- Untracked files after final commit — none
