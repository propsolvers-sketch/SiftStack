---
phase: quick-260901-rcs
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/entity_researcher.py
  - tests/test_entity_researcher.py
  - .github/workflows/daily-sweep.yml
autonomous: true
requirements: [QUICK-260901-RCS]
quick_task: true

must_haves:
  truths:
    - "The entity web-search query for an Alabama record carries the token 'Alabama', not the bare 'AL'"
    - "A Tennessee record still resolves to 'Tennessee' — generalization, not an AL-only swap"
    - "A record with an empty state resolves through its county instead of falling back to Tennessee"
    - "The nightly daily-sweep run actually reaches Step 3a (Entity Research) instead of skipping it"
  artifacts:
    - path: "src/entity_researcher.py"
      provides: "_resolve_search_state() helper + resolver-backed state wiring"
      contains: "from state_resolver import"
    - path: "tests/test_entity_researcher.py"
      provides: "No-network regression checks for AL/TN/empty-state resolution"
      contains: "_resolve_search_state"
    - path: ".github/workflows/daily-sweep.yml"
      provides: "--research-entities on the main.py daily invocation"
      contains: "--research-entities"
  key_links:
    - from: "src/entity_researcher.py"
      to: "src/state_resolver.py"
      via: "module import of state_full_name + state_for_county"
      pattern: "from state_resolver import"
    - from: "_research_single_entity"
      to: "_resolve_search_state"
      via: "direct call replacing the inline TN branch"
      pattern: "_resolve_search_state\\(notice\\)"
    - from: ".github/workflows/daily-sweep.yml"
      to: "enrichment_pipeline Step 3a"
      via: "main.py daily --research-entities -> skip_entity_research=False"
      pattern: "--research-entities"
---

<objective>
Turn on the already-built Entity Researcher in the nightly sweep, and fix the state token it
sends to web search so Alabama records search for "Alabama" instead of the ambiguous bare "AL".

Purpose: `src/entity_researcher.py` is 457 lines of finished, tested code that has never executed
in production — `skip_entity_research` defaults to `True` and only `--research-entities` flips it,
which the workflow never passes. Separately, its Phase 2 search path was written in the Knox/Blount
TN era: it expands `"TN"` to `"Tennessee"` but leaves `"AL"` (the `NoticeData` default and what every
AL pipeline sets) untouched, so every Alabama LLC/corp search goes out with a two-letter token that
degrades DuckDuckGo recall on exactly the population this feature targets.

Output: a resolver-backed `_resolve_search_state()` in `entity_researcher.py`, no-network regression
checks in the existing test script, and `--research-entities` on the workflow's main.py daily step.

Operational note (not scope): enabling this adds cost to the nightly run — one Claude Haiku call
(max 256 tokens) plus a DuckDuckGo search per entity candidate that misses the free name-parse fast
path, at 4 parallel workers with a 0.5-1.0s inter-search delay. Entity candidates are a small slice
of daily volume, so expect cents/day, but watch the first few runs.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@CLAUDE.md
@src/entity_researcher.py
@src/state_resolver.py
@tests/test_entity_researcher.py

<environment>
- Python interpreter is the project venv: `.venv/bin/python`. Bare `python` is NOT on PATH in a
  non-interactive shell. Every command in this plan uses `.venv/bin/python` explicitly.
- Run all commands from the project root (`/Users/shanismith/Desktop/SiftStack`). The test script
  self-inserts `src/` onto `sys.path`, so no PYTHONPATH export is needed.
- **`import anthropic` is extremely slow on a cold cache in a sandboxed shell** — a verified
  baseline run of `tests/test_entity_researcher.py` took 7m32s wall for 0.93s of user CPU (all
  blocking I/O during import, not test work). Give the test command a 600000ms timeout, or run it
  in the background and poll. Do not conclude the suite is hung.
</environment>

<interfaces>
<!-- Verified against the live codebase 2026-09-01. Use these directly; no exploration needed. -->

From src/state_resolver.py (pure stdlib, no external deps, safe to import anywhere):
```python
COUNTY_STATE: dict[str, str]          # {"jefferson": "AL", "madison": "AL", "marshall": "AL",
                                      #  "knox": "TN", "blount": "TN"}
DEFAULT_PROPERTY_STATE = "AL"

def state_for_county(county: str | None) -> str:
    """2-letter PROPERTY state for a county. Unknown/empty -> DEFAULT_PROPERTY_STATE ('AL')."""

def state_full_name(abbrev: str | None) -> str:
    """Full state name for a 2-letter abbrev. Empty/None/unknown -> 'Alabama'."""
```

Empirically confirmed return values (`.venv/bin/python`, 2026-09-01):
```
state_full_name('AL')        -> 'Alabama'
state_full_name('TN')        -> 'Tennessee'
state_full_name('al')        -> 'Alabama'     # case-insensitive, strips
state_full_name('')          -> 'Alabama'     # falls back to DEFAULT_PROPERTY_STATE
state_full_name(None)        -> 'Alabama'
state_full_name('Tennessee') -> 'Alabama'     # ⚠ LANDMINE — see below
state_for_county('Knox')      -> 'TN'
state_for_county('Jefferson') -> 'AL'
state_for_county('')          -> 'AL'
state_for_county(None)        -> 'AL'
```

⚠ **The landmine is load-bearing for this task.** `state_full_name()` looks its argument up in
`_STATE_LITERALS`, which is keyed by 2-letter abbrev only. A full name like `"Tennessee"` misses the
key, hits the unknown-abbrev branch, and silently returns `"Alabama"`. Passing an already-full state
name straight through `state_full_name()` would therefore corrupt TN records into Alabama searches —
the exact regression this task is supposed to prevent. Guard for length before calling.

From src/notice_parser.py (line 35 and line 48):
```python
@dataclass
class NoticeData:
    state: str = "AL"    # dataclass default; every AL pipeline also sets it explicitly
    county: str = ""
```

From src/entity_researcher.py — the two sites being changed:
```python
# line 158
def _search_entity(entity_name: str, state: str = "Tennessee") -> list[dict]:
    query = f'"{entity_name}" {state} registered agent OR member OR officer'   # line 163

# lines 325-329, inside _research_single_entity, Phase 2 only
    state = notice.state or "Tennessee"
    if state == "TN":
        state = "Tennessee"
    search_results = _search_entity(name, state)
```

Call-site survey (grep-verified, 2026-09-01): `_search_entity` has exactly ONE caller — line 329.
The only cross-module entry point into this file is `enrich_entity_data`, imported lazily at
`src/enrichment_pipeline.py:426`. Nothing else imports `_search_entity` or `_research_single_entity`.

From src/main.py:1348 — the CLI flag already exists on the top-level parser, so
`python src/main.py daily --research-entities` is valid argparse today:
```python
parser.add_argument(
    "--research-entities",
    action="store_true",
    help="Research entity-owned properties to find the person behind LLCs/Corps (web search + LLM)",
)
```
It is consumed at main.py lines 742, 821, 921, 2054 as
`skip_entity_research=not getattr(args, "research_entities", False)`.

From .github/workflows/daily-sweep.yml — `ANTHROPIC_API_KEY` is set at job level (line 82), so it
is already in scope for the main.py daily step. Without it, `enrich_entity_data` logs
"Web search: skipped (no API key)" and only the free name-parse path runs.
</interfaces>

<test_baseline>
`.venv/bin/python tests/test_entity_researcher.py` currently reports **47 passed, 0 failed** and
exits 0. It is a hand-rolled runner (a module-level `check(name, actual, expected)` function, a
printed summary, `sys.exit(1)` on any failure) — not pytest. Match that style exactly when adding
checks. After this plan the count must be **47 + (number of new checks), 0 failed**.
</test_baseline>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Resolve the entity search state through state_resolver instead of the TN literal</name>
  <files>src/entity_researcher.py, tests/test_entity_researcher.py</files>
  <behavior>
    Add these checks to tests/test_entity_researcher.py under a new
    `=== Search State Resolution ===` print header, placed after the existing
    "Name Parsing (Free Fast Path)" block and before "Entity Filter Exemption".
    Import `_resolve_search_state` by extending the existing
    `from entity_researcher import _classify_entity, _try_parse_entity_name` line.
    Every check constructs a `NoticeData(...)` with explicit kwargs and calls the pure function —
    zero network, zero API calls.

    - AL record: `NoticeData(state="AL", county="Jefferson")` -> `"Alabama"`   (the actual bug fix)
    - TN record: `NoticeData(state="TN", county="Knox")` -> `"Tennessee"`      (regression guard)
    - Empty state, TN county: `NoticeData(state="", county="Knox")` -> `"Tennessee"`
      (county fallback replaces the old blind Tennessee default)
    - Empty state, AL county: `NoticeData(state="", county="Jefferson")` -> `"Alabama"`
    - Empty state, empty county: `NoticeData(state="", county="")` -> `"Alabama"`
      (DEFAULT_PROPERTY_STATE)
    - Messy casing/whitespace: `NoticeData(state=" al ", county="")` -> `"Alabama"`
    - Already-full-name passthrough: `NoticeData(state="Tennessee", county="Knox")` -> `"Tennessee"`
      (guards the `state_full_name("Tennessee") -> "Alabama"` landmine documented in `<interfaces>`)
    - Default-param check, no network: `inspect.signature(_search_entity).parameters["state"].default`
      -> `""`  (proves the "Tennessee" literal default is gone). Import `inspect` and
      `_search_entity` at the top of the file alongside the other entity_researcher imports.
  </behavior>
  <action>
    Three edits in src/entity_researcher.py, all confined to the state wiring. Do not touch the
    entity classification regexes, ENTITY_EXTRACT_PROMPT, `_try_parse_entity_name`, the cache, the
    ThreadPoolExecutor, or `enrich_entity_data`.

    1. Add `from state_resolver import state_for_county, state_full_name` to the import block
       (alongside `import config` / `from notice_parser import NoticeData` — flat imports, because
       src/ is the working directory for this codebase). `state_resolver` is pure stdlib, so this
       introduces no circular-import or dependency risk.

    2. Add a module-level helper `_resolve_search_state(notice: NoticeData) -> str` in the
       "Web Search" section, immediately above `_search_entity`. Logic, in order: strip
       `notice.state`; if the result is empty, derive an abbreviation via
       `state_for_county(notice.county)`; if the value is longer than 2 characters treat it as an
       already-expanded state name and return it unchanged (this is the guard against the
       `state_full_name("Tennessee") -> "Alabama"` landmine — deliberately do NOT import
       `state_resolver._normalize_state_name`, which is private); otherwise return
       `state_full_name(value)`. Docstring should say it returns the full state name for the search
       query ("Alabama", not "AL") and cite that DuckDuckGo recall is the reason. Do NOT build any
       new abbreviation-to-name mapping — `state_resolver` owns that.

    3. Rewire the two call sites. In `_research_single_entity`, delete the three lines
       `state = notice.state or "Tennessee"` / `if state == "TN":` / `state = "Tennessee"` and pass
       `_resolve_search_state(notice)` into `_search_entity`. In `_search_entity`, change the
       signature default from `"Tennessee"` to `""`, and inside the function resolve the token as
       the passed-in stripped state or, when empty, `state_full_name(None)` (which yields the
       DEFAULT_PROPERTY_STATE full name, "Alabama") before interpolating it into `query`. The query
       f-string shape itself stays byte-identical apart from the substituted token.

    Then add the checks from `<behavior>` to tests/test_entity_researcher.py in the existing
    hand-rolled `check(...)` style. Do not convert the file to pytest.
  </action>
  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack && .venv/bin/python tests/test_entity_researcher.py; echo "exit=$?"</automated>
    <automated>cd /Users/shanismith/Desktop/SiftStack && grep -vE '^\s*#' src/entity_researcher.py | grep -Ec '"Tennessee"' || echo 0</automated>
    <automated>cd /Users/shanismith/Desktop/SiftStack && grep -c 'from state_resolver import' src/entity_researcher.py && grep -c '_resolve_search_state(notice)' src/entity_researcher.py</automated>
  </verify>
  <done>
    Test script prints "55 passed, 0 failed" (47 baseline + 8 new) and exits 0. The
    `"Tennessee"` string-literal count in non-comment lines of src/entity_researcher.py is 0. Both
    grep -c link checks return 1. Expect the test run to take up to ~8 minutes of wall time on a
    cold `anthropic` import — use a 600000ms timeout, that is not a hang.
  </done>
</task>

<task type="auto">
  <name>Task 2: Pass --research-entities on the workflow's main.py daily step</name>
  <files>.github/workflows/daily-sweep.yml</files>
  <action>
    In the step named "Run main.py daily (foreclosure + probate)" (around line 163), add
    `--research-entities \` to the `python src/main.py daily` invocation, inserted between the
    `--tiers 1,2 \` line and the `--no-raw-csv \` line. Preserve the existing two-space-per-level
    indentation and the trailing backslash continuation style used by the surrounding flags — a
    dropped or misaligned backslash silently truncates the command in the runner.

    Only this step. Do NOT add the flag to the Rubin Lublin, Tiffany & Bosco pending, T&B Results,
    Halliday Watkins Mann, pre-probate, AdHunter, code-violation, or any other pipeline step —
    `main.py daily` is the only path that reaches `enrichment_pipeline` Step 3a, and the trustee
    adapters do not accept the argument. Change nothing else in the file: no schedule edits, no
    env/secret edits, no touching the `--skip-trace` flags on the trustee steps.

    `ANTHROPIC_API_KEY` is already a job-level env var (line 82), so no secret wiring is required.
  </action>
  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack && .venv/bin/python -c "import yaml; d=yaml.safe_load(open('.github/workflows/daily-sweep.yml')); print('YAML OK, jobs:', list(d['jobs']))"</automated>
    <automated>cd /Users/shanismith/Desktop/SiftStack && grep -c -- '--research-entities' .github/workflows/daily-sweep.yml</automated>
    <automated>cd /Users/shanismith/Desktop/SiftStack && grep -A 16 'Run main.py daily (foreclosure + probate)' .github/workflows/daily-sweep.yml | grep -c -- '--research-entities'</automated>
  </verify>
  <done>
    The workflow parses as valid YAML and reports the `daily-sweep` job. `--research-entities`
    appears exactly once in the whole file (first grep returns 1) and that single occurrence is
    inside the main.py daily step (second grep returns 1). The flag matches the one registered at
    src/main.py:1348, so argparse accepts it unchanged.
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| SiftStack -> DuckDuckGo (`ddgs`) | Outbound: entity names from public records leave the host in a search query |
| SiftStack -> Anthropic API | Outbound: search-result snippets sent to Claude Haiku for extraction |
| GitHub Actions runner -> both of the above | Enabling the flag moves both calls from local-only into nightly CI |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-RCS-01 | Information Disclosure | `_search_entity` outbound DDG query | accept | Entity names are already public record (tax roll / newspaper legal notices). No PII, no phones, no addresses in the query — only the entity name plus a state token. |
| T-RCS-02 | Information Disclosure | `_parse_entity_with_llm` -> Anthropic | accept | Only public search-result snippets are sent. Same posture as the already-shipped obituary and notice-parser Haiku paths. |
| T-RCS-03 | Denial of Service | Nightly cost/rate exposure once the flag is live | mitigate | Existing controls stay untouched and unchanged by this plan: free name-parse fast path runs first, `PARALLEL_WORKERS = 4`, `SEARCH_DELAY_MIN/MAX` 0.5-1.0s jitter, per-run `search_cache` dedup by uppercased entity name, `MAX_TOKENS = 256`. Step is `continue-on-error: true`, so a DDG rate-limit cannot fail the sweep. |
| T-RCS-04 | Tampering | LLM-returned `person_name` written onto NoticeData | accept | Pre-existing behavior, unchanged by this plan. Output is confidence-tagged (`entity_research_confidence`) and source-tagged (`entity_research_source`) for downstream filtering. |
| T-RCS-SC | Tampering | npm/pip/cargo installs | n/a | No new packages. `state_resolver` is an existing first-party pure-stdlib module; `ddgs` and `anthropic` are already imported by this file today. No install step, so no legitimacy gate applies. |
</threat_model>

<verification>
1. `.venv/bin/python tests/test_entity_researcher.py` -> 55 passed, 0 failed, exit 0
   (allow up to ~8 min wall on cold `anthropic` import).
2. AL resolves to "Alabama" and TN still resolves to "Tennessee" — both asserted in the suite,
   proving this is a generalization rather than an AL-only swap.
3. No `"Tennessee"` string literal remains in non-comment lines of `src/entity_researcher.py`.
4. `.github/workflows/daily-sweep.yml` parses as valid YAML with `--research-entities` present
   exactly once, on the main.py daily step only.
5. No live DuckDuckGo search and no Anthropic API call is made during verification — every check
   exercises pure functions or greps text.
</verification>

<success_criteria>
- `_resolve_search_state()` exists in `src/entity_researcher.py` and derives the full state name
  entirely from `state_resolver` (`state_full_name` + `state_for_county`), with no new
  abbreviation mapping introduced anywhere.
- Alabama records search with "Alabama"; Tennessee records still search with "Tennessee";
  empty-state records resolve through their county instead of defaulting to Tennessee.
- The already-full-name input case is guarded, so the `state_full_name("Tennessee") -> "Alabama"`
  landmine cannot corrupt a TN record.
- `tests/test_entity_researcher.py` covers all of the above with no network access and passes.
- The nightly sweep's main.py daily step passes `--research-entities`, so
  `skip_entity_research` becomes False and `enrichment_pipeline` Step 3a executes in production
  for the first time.
- Entity classification regexes, the LLM prompt, the name-parse fast path, the search cache, and
  the threading model are byte-for-byte unchanged.
</success_criteria>

<output>
Create `.planning/quick/260901-rcs-enable-entity-researcher-with-alabama-st/260901-rcs-SUMMARY.md` when done.

Include in the summary: the final test pass count, confirmation that both the AL and TN resolution
checks pass, and a note that the first post-merge nightly sweep should be checked for the
"── Step 3a: Entity Research (N candidates) ──" log line plus its
"Name-parsed: N / Web search: N/M" counters to confirm the feature is live and to size actual cost.
</output>
