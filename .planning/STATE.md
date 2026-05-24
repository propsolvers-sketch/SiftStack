# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-05-23)

**Core value:** Convert public-record distress signals into actionable, skip-traced, in-tier, DM-attached leads inside DataSift on a low-cost daily cadence at sub-cent-per-record economics.
**Current focus:** Phase 1 — Stabilize Production (v1.1 active milestone)

## Current Position

Phase: 1 of 5 (Stabilize Production)
Plan: 0 of 4 in current phase
Status: Planned (Ready to execute)
Last activity: 2026-05-24 — Phase 1 planned: 4 plans, 2 waves (BUGFIX-02 → BUGFIX-01 + BUGFIX-03 + PARSER-01). All 4 fixes already in production source per plan-checker spot-check — Phase 1 deliverable is the regression test net (golden tests in new `tests/unit/`).

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 1. Stabilize Production | 0/TBD | — | — |

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

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260523-uvu | Move Madison/Marshall Smarty-zip geocode helpers into shared address_standardizer.py and wire them into the legacy main.py probate flow via property_lookup.py | 2026-05-24 | 07a48fa | [260523-uvu-move-madison-marshall-smarty-zip-geocode](./quick/260523-uvu-move-madison-marshall-smarty-zip-geocode/) |

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
