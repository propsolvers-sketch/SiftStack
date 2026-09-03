# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-05-23)

**Core value:** Convert public-record distress signals into actionable, skip-traced, in-tier, DM-attached leads inside DataSift on a low-cost daily cadence at sub-cent-per-record economics.
**Current focus:** Phase 1 — Stabilize Production (v1.1 active milestone)

## Current Position

Phase: 2 of 5 (Funnel Transparency) — ✅ Complete (status: partial — 3/4 SCs + 1 documented deferral)
Plan: 5 of 5 in current phase
Status: Verified (ready for Phase 3)
Last activity: 2026-09-03 — Completed quick task 260903-kh6: normalized-address fallback for DataSift UUID lookup. Watch the next sweep for `DS_ADDR_FALLBACK` lines (how often the fallback fires) and for `Ambiguous normalized address key` warnings (collisions where it correctly refused to guess).

Prior activity: 2026-09-02 — Completed quick task 260901-rcs: Enable Entity Researcher with Alabama state wiring. Step 3a (entity research) now runs on the nightly sweep for the first time; watch the first post-merge run for `── Step 3a: Entity Research (N candidates) ──` and its `Name-parsed: N / Web search: N/M` counters to confirm it is live and to size real Haiku+DDG spend.

Prior activity: 2026-05-24 — Phase 2 complete: 5 plans executed across 3 waves, 110 tests pass + 1 documented skip. All 6 pipelines (main_daily 10g, apn_probate 6g, pre_probate 9g, benchmark 6g, tax_distress 5g, code_violation 3g) wired with FunnelCounter + ServiceRateTracker via additive kwargs. Each pipeline emits one Slack message (summary + funnel block + service-rates block) per CONTEXT.md D-02. Today's per-run rate + 7-day rolling baseline rendered side-by-side per D-03. SC-4 (yellow-warning alert thresholds) intentionally deferred per CONTEXT.md D-04 ("Phase 2 emits numbers; humans decide what's bad") — substrate is in place, threshold logic is a Phase 5+ enhancement.

Phase 1 closeout summary: 4 plans, 35 tests pass + 1 documented skip, verification status: passed (5/5 success criteria), zero production source modified — fixes were already in place.

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 1. Stabilize Production | 4/4 ✅ | ~25 min | ~6 min |
| 2. Funnel Transparency | 5/5 ✅ | ~3h | ~36 min |

**Recent Trend:**
- Last 5 plans: (none yet — v1.1 milestone just initialized)
- Trend: —

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Single canonical `NoticeData` schema across all ingestion paths (locks downstream consumers — append-only)
- DataSift integration is Playwright UI automation, not REST (no API exists)
- Tier 1/Tier 2 ZIP defs live in BOTH `src/target_zips.py` AND `~/Documents/Claude/Projects/REI Skill Library/*_County_AL_SFR_*_Market_Analysis.md` — keep in sync
- Use Apify KVS for production state; local JSON files for single-developer CLI (drift between `actor_main()` + `cli_main()` is the root cause of BUGFIX-02)
- APN scraper is canonical Madison post-probate path (Madison portal is recording-only)

### Pending Todos

None yet.

### Blockers/Concerns

- BUGFIX-02 (Apify cold-start `AttributeError`) blocks any daily-Apify deployment of current code — every scheduled run dies before scraping. Resolved in Phase 1.
- Marshall tax-delinquent feed disabled by county; stub raises `NotImplementedError` if page comes back online (by design). Phase 4 back-fill triggers when county re-enables.
- Tier ZIP defs in 2 places (`src/target_zips.py` + REI Skill Library MD analysis docs) — manual sync risk; carry forward as recurring hygiene concern.
- 4,246-line `datasift_uploader.py` + 1,905-line `main.py` are tech-debt monoliths deferred to v2 (REFAC-03 / REFAC-05).
- Tesseract is not in Dockerfile; `photo-import` / `pdf-import` will fail on Apify. Daily scrape unaffected; v2 fix (REFAC-06).
- Phase 2 deferred to Phase 3: `apn_probate_pipeline_al.run_pipeline` doesn't yet pass `rate_tracker` into `scrape_all` (stale inline TODO); `code_violation_pipeline.py` has no rate_tracker threads (adapters use internal property API paths, not the instrumented `address_standardizer`). Pure-single-pipeline CLI runs render "n/a today" for affected services. main.py daily path is wired correctly. Resolve when Phase 3 consolidates.
- Phase 3 service-rate merge must sum `tracker.totals()` across pipelines BEFORE deriving per-run rate (don't average per-pipeline rates). `save_rolling_rates` should be called once per day, not 6 times.

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260523-uvu | Move Madison/Marshall Smarty-zip geocode helpers into shared address_standardizer.py and wire them into the legacy main.py probate flow via property_lookup.py | 2026-05-24 | 07a48fa | [260523-uvu-move-madison-marshall-smarty-zip-geocode](./quick/260523-uvu-move-madison-marshall-smarty-zip-geocode/) |
| 260525-ucl | Rename `tn_notices_*.csv` → `al_notices_*.csv` (3 string refs in `data_formatter.py` + `main.py`) — Phase 2 validation surfaced the stale TN-era CSV name on today's all-AL daily run | 2026-05-26 | fb9d953 | [260525-ucl-tn-to-al-csv-filename](./quick/260525-ucl-tn-to-al-csv-filename/) |
| 260525-vop | Add surgical ModalOverlay+Loom dismissal to `dismiss_popups()` in `src/datasift_core.py` — fixes the GHA daily-sweep "Uploaded 0/1 splits" failure on all 3 DataSift uploads (root cause: fresh GHA browser → no `.datasift_profile/` → DataSift treats session as first-time user → Loom-video onboarding modal intercepts every wizard click) | 2026-05-26 | 08042b7 | [260525-vop-modaloverlay-loom-dismissal](./quick/260525-vop-modaloverlay-loom-dismissal/) |
| 260829-pat | Correct `daily-sweep.yml` line 9 PAT expiry comment (2026-08-27 → 2027-08-28) after rotating the cron-job.org fine-grained token. The old PAT expired 08-27 and silently killed the 08-28 + 08-29 daily sweeps — cron-job.org logged `401 Unauthorized` at 2:30 AM both days and no GHA run was created at all. Token regenerated (not deleted/recreated, so repo scope + Actions:R+W were preserved), cron-job.org `Authorization` header updated, dispatch re-verified end-to-end (204 → run 33256569831). Calendar reminder set for 2027-08-14. | 2026-08-29 | 5c42d8c | — (fast task, no plan dir) |
| 260525-vwk | Thread `rate_tracker` through `notice_parser.parse_notice_page` so legacy main.py daily flow records LLM service rate (closes the `LLM: n/a today` cell in Phase 2 Slack block). 4 additive edits — adds keyword-only kwarg to parse_notice_page signature + threads it to both internal LLM calls + updates scraper call site. Closes Phase 2 VERIFICATION carry-forward item #3 (LLM 0/0 service_rates.json gap). | 2026-05-26 | 4a52e4c | [260525-vwk-llm-rate-tracker-plumb](./quick/260525-vwk-llm-rate-tracker-plumb/) |
| 260901-rcs | Enable Entity Researcher in production with Alabama state wiring. `src/entity_researcher.py` (457 lines) was fully built and wired into `enrichment_pipeline.py:426` (Step 3a) but had never executed — `skip_entity_research` defaults True and only `--research-entities` flips it, which the nightly sweep never passed. Two changes: (1) new `_resolve_search_state(notice)` resolves `notice.state` → `state_for_county(county)` → `state_full_name()`, replacing the hardcoded `state = notice.state or "Tennessee"` / `if state == "TN"` block, so AL records search `"Alabama"` instead of the bare ambiguous `AL` token that was degrading DuckDuckGo recall on exactly the LLC/corp population this targets; `_search_entity` default moved off `"Tennessee"` to `""`. A `len(value) > 2` guard is load-bearing — `state_full_name` keys on 2-letter abbrevs only, so feeding it `"Tennessee"` misses the key and silently returns `"Alabama"`. (2) `--research-entities` added to the `main.py daily` step in `daily-sweep.yml`. TDD: RED `4a0b7f5` → GREEN `18e5e61`; suite 47 → 55 passed, 0 failed. | 2026-09-02 | 9baf025 | [260901-rcs-enable-entity-researcher-with-alabama-st](./quick/260901-rcs-enable-entity-researcher-with-alabama-st/) |

| 260903-kh6 | Normalized-address fallback for DataSift property UUID lookup. `_property_index_key` was an exact lowercase match, so the T&B portal's `807 Briarwood Drive` never matched DataSift's stored `807 Briarwood Dr` — confirmed live in run 33728416332 (`no DataSift match for 807 Briarwood Dr (subtype=foreclosure_cancelled)`), so the `foreclosure_cancelled` tag never landed and the operator couldn't filter those records (subtype reached Notes only, and DataSift can't filter note text). Fix: new zero-dep `src/street_suffixes.py` (SUFFIX_ABBR moved verbatim from `al_property_enricher`, which keeps a `_SUFFIX_ABBR` alias) + `canonical_street()`; `build_property_index` now builds a second suffix-normalized index in the SAME pagination pass; `find_property_uuid_by_address` tries the exact key first (unchanged fast path) and falls back to the normalized key only on a miss, logging `DS_ADDR_FALLBACK`. Ambiguity guard: two DISTINCT UUIDs collapsing onto one normalized key returns None rather than guessing (same UUID under two spellings is not ambiguous). Fixes 4 silently-degraded callers: apply_subtype_tags, apply_courthouse_snapshots, capture_today_probate_uuids (empty Path A file is the suspected cause of the 5h SmartSkip step on 2026-09-03), capture_today_standard_uuids. Tests 24 new + 55 existing pass. | 2026-09-03 | d5afd53 | [260903-kh6-normalized-address-fallback-for-datasift](./quick/260903-kh6-normalized-address-fallback-for-datasift/) |

## Deferred Items

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Tech debt | CountyPropertyAdapter Protocol refactor (REFAC-01) | Deferred to v2 | 2026-05-23 |
| Tech debt | Consolidate 4 name-splitter implementations (REFAC-02) | Deferred to v2 | 2026-05-23 |
| Tech debt | Split datasift_uploader.py monolith (REFAC-03) | Deferred to v2 | 2026-05-23 |
| Tech debt | Factor run_full_pipeline() shared helper (REFAC-04) | Deferred to v2 | 2026-05-23 |
| Tech debt | Mode registry pattern for main.py (REFAC-05) | Deferred to v2 | 2026-05-23 |
| Infra | Add tesseract-ocr to Dockerfile (REFAC-06) | Deferred to v2 | 2026-05-23 |
| Infra | requirements.lock + upper-bound pinning (REFAC-07) | Deferred to v2 | 2026-05-23 |
| Tests | Move tests to tests/integration + tests/unit (TEST-01/02/03) | Deferred to v2 | 2026-05-23 |
| Coverage | Additional AL counties (Shelby/Lee/Tuscaloosa/Mobile/Baldwin) | Deferred to v2 | 2026-05-23 |
| Coverage | Eviction + divorce via APN (currently photo-only) | Deferred to v2 | 2026-05-23 |
| Security | Bundled CA cert vs verify=False (SEC-V2-01) | Deferred to v2 | 2026-05-23 |

## Session Continuity

Last session: 2026-05-23 (doc-ingest bootstrap)
Stopped at: PROJECT.md / REQUIREMENTS.md / ROADMAP.md / STATE.md written from `.planning/intel/` synthesis + `.planning/codebase/` cross-check
Resume file: None (next action is `/gsd-plan-phase 1`)
