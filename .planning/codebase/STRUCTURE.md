# Codebase Structure

**Analysis Date:** 2026-04-30

## Directory Layout

```
SiftStack/
├── src/                  # All Python source code (54 modules)
├── tests/                # Pytest tests + manual ground-truth fixtures + tools
├── output/               # Generated CSVs (gitignored — runtime artifacts)
│   └── reports/          # Per-record deep-prospecting PDFs (created at runtime)
├── logs/                 # Date-stamped scrape logs (gitignored)
├── docs/
│   └── foia/             # FOIA letter templates for code-enforcement data
├── .actor/               # Apify Actor deployment manifest + input schema
├── .planning/
│   └── codebase/         # GSD codebase mapping documents (this directory)
├── .venv/                # Local Python virtualenv (gitignored)
├── .env                  # Credentials (gitignored)
├── .env.example          # Template for required env vars
├── .gitignore
├── .apifyignore          # Files excluded from Apify build
├── CLAUDE.md             # Per-project Claude Code guidance (~82 KB)
├── README.md             # User-facing docs (~17 KB)
├── LICENSE               # MIT
├── Dockerfile            # Apify Actor base image (apify/actor-python-playwright:3.12)
├── requirements.txt      # Pip dependencies
├── input.json            # Local Apify input for `apify run` (gitignored)
├── last_run.json         # Persisted "last scrape date" — daily-mode cutoff
├── last_run.json.bak     # Backup of last_run.json
├── seen_ids.json         # Cross-run notice-ID dedup cache (90-day prune)
├── seen_ids.json.bak     # Backup of seen_ids.json
├── captcha_failed_ids.json  # Notice IDs that exhausted CAPTCHA retries (14-day prune)
├── cookies.json          # APN session cookies (gitignored)
├── dropbox_state.json    # Dropbox cursor + processed-file state (gitignored)
├── photo_state.json      # Photo dedup state for Dropbox watcher (gitignored)
└── test_*.py             # Top-level integration / manual test scripts
                          # (test_ancestry.py, test_datasift_upload.py, test_e2e_*.py,
                          #  test_entity_upload.py, test_existing_list_upload.py,
                          #  test_manage_presets.py, test_manage_sold.py,
                          #  test_phone_validator.py, test_tracerfy_*.py)
```

## Directory Purposes

**`src/`:**
- Purpose: All production Python source code
- Contains: Entry point, scraper, parsers, enrichers, county adapters, orchestrators, formatters, uploaders, analysis tools
- Key files: `src/main.py` (CLI + Apify entry), `src/__main__.py` (module entry for `python -m src`), `src/config.py` (env + selectors + searches), `src/notice_parser.py` (canonical schema), `src/enrichment_pipeline.py` (10-step pipeline orchestrator)
- Convention: All modules import each other directly (no package structure); imports assume `src/` is on `PYTHONPATH`

**`tests/`:**
- Purpose: Pytest tests + manual ground-truth fixtures + maintenance scripts
- Contains: Unit tests (`test_parser.py`, `test_parser_edge_cases.py`, `test_pdf_importer.py`, `test_obituary_enricher.py`, `test_deceased_detection.py`, `test_entity_researcher.py`, `test_e2e_obituary.py`, `test_captcha_live.py`); ground-truth JSON (`obituary_ground_truth.json`); maintenance scripts (`reenrich.py`, `merge_foreclosure.py`, `run_obituary_enrichment.py`, `run_dm_address_backfill.py`, `export_review_xlsx.py`, `export_template_xlsx.py`)
- Key files: `tests/README.md` documents the obituary ground-truth dataset
- Convention: `test_*.py` for pytest; `run_*.py` and `export_*.py` for one-off maintenance scripts

**Top-level integration tests (`/test_*.py`):**
- Purpose: End-to-end and manual integration tests that drive Playwright and live APIs
- Contains: `test_ancestry.py`, `test_datasift_upload.py` (headed browser DataSift upload + enrich + skip trace), `test_e2e_record.py`, `test_e2e_smyth.py`, `test_entity_upload.py`, `test_existing_list_upload.py`, `test_manage_presets.py`, `test_manage_sold.py`, `test_phone_validator.py`, `test_tracerfy_discovery.py`, `test_tracerfy_upload.py`
- Convention: Live integration tests live at the project root (run manually); deterministic unit tests live under `tests/`

**`output/`:**
- Purpose: Generated CSVs and PDFs (gitignored)
- Contains: Sift-format CSVs (`tn_notices_YYYY-MM-DD_HHMMSS.csv`, `{county}_{type}_YYYY-MM-DD_HHMMSS.csv` for split mode), DataSift-format CSVs (`datasift_*.csv`), Market Finder JSON exports
- Key files: `output/reports/` subdirectory holds per-record deep-prospecting PDFs (`{address}.pdf`)
- Generated: Yes (every run)
- Committed: No (`.gitignore`)

**`logs/`:**
- Purpose: Date-stamped scrape logs from CLI runs
- Contains: `scrape_YYYY-MM-DD_HHMMSS.log`
- Generated: Yes (every CLI run)
- Committed: No (`.gitignore`)

**`docs/`:**
- Purpose: User-facing documentation that doesn't fit in CLAUDE.md or README.md
- Contains: `docs/foia/huntsville_code_enforcement_request.md` (Alabama Open Records Act letter template)
- Generated: No
- Committed: Yes

**`.actor/`:**
- Purpose: Apify Actor deployment configuration
- Contains: `actor.json` (manifest, dataset views, name + version), `input_schema.json` (Apify Console UI fields + validation)
- Generated: No
- Committed: Yes

**`.planning/codebase/`:**
- Purpose: GSD-generated codebase mapping documents (consumed by other GSD commands)
- Contains: `ARCHITECTURE.md`, `STRUCTURE.md`, plus future `STACK.md`/`INTEGRATIONS.md`/`CONVENTIONS.md`/`TESTING.md`/`CONCERNS.md`
- Generated: Yes (by `/gsd-map-codebase`)
- Committed: Optional (project convention)

**`.venv/`:**
- Purpose: Local Python virtualenv
- Generated: Yes (developer-local)
- Committed: No

## Key File Locations

**Entry Points:**
- `src/main.py`: CLI dispatch + Apify Actor `actor_main()` (1905 lines, the largest entry-point file)
- `src/__main__.py`: `python -m src` shim that runs `actor_main()` for the Apify Actor
- `Dockerfile`: Apify Actor base image, `CMD ["python", "src/main.py"]`
- `src/tax_distress_pipeline.py`: Direct CLI for tax-distress pull
- `src/code_violation_pipeline.py`: Direct CLI for code-violation pull
- `src/extract_market_finder.py`: Direct CLI for Market Finder extraction
- `src/probate_property_locator.py`: Direct CLI for ad-hoc decedent property lookup
- Each `src/*_api.py` county adapter: own `__main__` for ad-hoc testing

**Configuration:**
- `src/config.py`: Env-var loading, ASP.NET selectors, `SAVED_SEARCHES`, regex patterns, paths
- `.env`: Runtime credentials (gitignored — see `.env.example`)
- `.env.example`: Documented template of required env vars
- `.actor/input_schema.json`: Apify Console input field definitions
- `requirements.txt`: Pip dependencies (Python 3.12+)

**Core Logic:**
- `src/notice_parser.py`: Canonical `NoticeData` dataclass + regex-based parsers + `parse_notice_page()` async function (1754 lines)
- `src/enrichment_pipeline.py`: 10-step pipeline orchestrator + `PipelineOptions` dataclass (729 lines)
- `src/scraper.py`: Playwright APN scraper, ASP.NET ViewState navigation (621 lines)
- `src/captcha_solver.py`: 2Captcha API integration for reCAPTCHA v2 (154 lines)
- `src/foreclosure_filter.py`: INCLUDE/EXCLUDE phrase filters for trustee-sale notices (113 lines)
- `src/llm_parser.py` + `src/llm_client.py`: Claude Haiku LLM fallback parsing (283 + 333 lines)
- `src/image_utils.py`: Shared OCR + rotation helpers used by both PDF and photo importers (55 lines)

**Per-Domain Orchestrators:**
- `src/tax_distress_pipeline.py`: Madison + Jefferson tax adapters
- `src/code_violation_pipeline.py`: Huntsville + Birmingham code adapters

**Enrichers:**
- `src/address_standardizer.py`: Smarty
- `src/property_enricher.py`: Zillow via OpenWeb Ninja
- `src/tax_enricher.py`: Knox Tax API + parcel lookup
- `src/obituary_enricher.py`: Obituary search + heir/DM extraction (3030 lines, largest module)
- `src/ancestry_enricher.py`: Ancestry.com SSDI via Playwright
- `src/entity_researcher.py`: LLC/Corp → person extraction
- `src/probate_property_locator.py`: Multi-county probate property lookup chain

**County Adapters:**
- `src/madison_property_api.py`: AssuranceWeb owner + situs-address search
- `src/jefferson_property_api.py`: E-Ring Capture API search
- `src/madison_tax_delinquent_api.py`: Bulk tax-delinquent feed
- `src/jefferson_tax_delinquent_api.py`: Birmingham + Bessemer tax-sale rosters
- `src/huntsville_unsafe_buildings_api.py`: Monthly unsafe-buildings PDF parser
- `src/birmingham_code_enforcement_api.py`: Accela Citizen Access Playwright scraper
- `src/property_lookup.py`: Knox Tax API name-search for probate

**Formatters & Uploaders:**
- `src/data_formatter.py`: Sift CSV columns + dedup
- `src/datasift_formatter.py`: DataSift 80-column CSV + tag generation
- `src/datasift_core.py`: Shared DataSift Playwright login + popup helpers
- `src/datasift_uploader.py`: Playwright upload wizard + enrich + skip trace + presets + sequences (4246 lines, second-largest module)
- `src/drive_uploader.py`: Google Drive upload via service account
- `src/dropbox_uploader.py`: Dropbox upload + share-link generation
- `src/slack_notifier.py`: Slack/Discord webhook posting
- `src/report_generator.py`: Per-record deep-prospecting PDFs (reportlab)
- `src/excel_exporter.py`: 7-sheet Knox market reports

**Analysis Tools (parallel layer):**
- `src/comp_analyzer.py`: Two-Bucket ARV
- `src/rehab_estimator.py`: 4-tier rehab cost
- `src/deal_analyzer.py`: MAO + financing scenarios
- `src/market_analyzer.py`: 6-factor zip code scoring
- `src/buyer_prospector.py`: Cash buyer list builder
- `src/deep_prospector.py`: 4-level research depth coordinator
- `src/lead_manager.py`: 4 Pillars + STABM
- `src/niche_sequential.py`: 12 + 9 marketing preset compositions
- `src/sequence_templates.py`: 26 TCA sequence definitions
- `src/playbook_generator.py`: SOP + script + checklist generator

**Importers (file → NoticeData):**
- `src/pdf_importer.py`: Tax-sale PDF OCR
- `src/photo_importer.py`: Courthouse-photo OCR
- `src/dropbox_watcher.py`: Auto-poll cycle wrapping `photo_importer`

**Testing:**
- `tests/test_parser.py`, `tests/test_parser_edge_cases.py`: Notice parser regex tests
- `tests/test_pdf_importer.py`: PDF OCR pipeline tests
- `tests/test_obituary_enricher.py`, `tests/test_e2e_obituary.py`: Obituary enrichment tests
- `tests/test_deceased_detection.py`: Deceased indicator regex tests
- `tests/test_entity_researcher.py`: Entity researcher tests
- `tests/test_captcha_live.py`: Live 2Captcha integration test
- `tests/obituary_ground_truth.json`: Ground-truth fixture data

**Runtime State Files (project root):**
- `last_run.json`: Last successful scrape date (daily-mode cutoff)
- `seen_ids.json`: Cross-run notice-ID dedup cache (90-day prune window)
- `captcha_failed_ids.json`: Notice IDs that exhausted CAPTCHA retries (14-day prune)
- `cookies.json`: APN Playwright session cookies
- `dropbox_state.json`: Dropbox API cursor for incremental polling
- `photo_state.json`: Processed-photo dedup hashes
- `*.json.bak`: Backup snapshots written before atomic state writes (see `src/config.py:save_state`)

## Naming Conventions

**Files:**
- `*_api.py`: Per-county external-data adapter (e.g. `madison_property_api.py`, `jefferson_tax_delinquent_api.py`, `huntsville_unsafe_buildings_api.py`, `birmingham_code_enforcement_api.py`). Each exposes `fetch_*()` plus `to_notice_data()` and a per-record dataclass.
- `*_pipeline.py`: Cross-county orchestrator (e.g. `tax_distress_pipeline.py`, `code_violation_pipeline.py`, `enrichment_pipeline.py`). Each exposes a public `fetch_*()` or `run_*_pipeline()` and a CLI `_main(argv)`.
- `*_enricher.py`: Adds fields to existing `NoticeData` records in-place (e.g. `obituary_enricher.py`, `ancestry_enricher.py`, `tax_enricher.py`). Receives `list[NoticeData]`, mutates in place, returns `None` or count.
- `*_importer.py`: Reads files (PDFs, photos) and produces `list[NoticeData]` (e.g. `pdf_importer.py`, `photo_importer.py`).
- `*_formatter.py`: Converts `NoticeData` → CSV / output format (e.g. `data_formatter.py`, `datasift_formatter.py`).
- `*_uploader.py`: Pushes data to external services (e.g. `datasift_uploader.py`, `drive_uploader.py`, `dropbox_uploader.py`).
- `*_analyzer.py`: Standalone analysis tool reading from `output/` CSVs (e.g. `comp_analyzer.py`, `market_analyzer.py`, `deal_analyzer.py`).
- `*_generator.py`: Produces a derived artifact (e.g. `report_generator.py` → PDFs, `playbook_generator.py` → Word docs).
- `*_watcher.py`: Long-running poll loop (e.g. `dropbox_watcher.py`).
- `*_solver.py`: External challenge-solving integration (e.g. `captcha_solver.py`).
- `*_tracer.py`: Skip-tracing integrations (e.g. `tracerfy_skip_tracer.py`).
- `*_validator.py`: External validation services (e.g. `phone_validator.py`).
- `*_locator.py`: Multi-tier lookup chains (e.g. `probate_property_locator.py`).
- `extract_*.py`: Playwright/scraping module that's NOT an adapter — pulls one-off datasets (e.g. `extract_market_finder.py`).
- `test_*.py`: Pytest tests (under `tests/`) or manual integration scripts (project root).
- `run_*.py`: One-off maintenance scripts (under `tests/`).
- `export_*.py`: Tooling that writes auxiliary export formats (under `tests/`).

**Directories:**
- Lowercase, no separators (`src`, `tests`, `output`, `logs`, `docs`)
- Dot-prefixed for tooling/config (`.actor`, `.planning`, `.venv`, `.git`)

**Functions:**
- Public: `snake_case` (e.g. `fetch_tax_distress`, `run_enrichment_pipeline`, `enrich_obituary_data`)
- Private/helper: `_snake_case` with leading underscore (e.g. `_filter_searches`, `_run_pdf_import`, `_fetch_madison`, `_validate_records`)
- CLI builders: `_build_argparser()` + `_main(argv)` + `_summarize(notices)` in pipeline modules
- `to_notice_data(rec)`: Convention for per-county adapter → `NoticeData` converter

**Classes / dataclasses:**
- `PascalCase` (e.g. `NoticeData`, `PipelineOptions`, `SearchConfig`, `MadisonPropertyRecord`, `JeffersonDelinquentRecord`)

**Constants:**
- `UPPER_SNAKE_CASE` (e.g. `BASE_URL`, `SAVED_SEARCHES`, `MAX_RETRIES`, `RECAPTCHA_SITEKEY`, `BUSINESS_RE`)
- Selectors: prefixed `SEL_` (e.g. `SEL_SEARCH_TEXT`, `SEL_RESULTS_GRID`)
- Compiled regexes: `_NAME_RE` style with optional leading underscore for module-private

**Output files:**
- Sift CSV: `tn_notices_YYYY-MM-DD_HHMMSS.csv` (default) or `{county}_{type}_YYYY-MM-DD_HHMMSS.csv` (`--split`)
- PDF reports: `output/reports/{address}.pdf`
- Logs: `logs/scrape_YYYY-MM-DD_HHMMSS.log`
- Market Finder: `output/market_finder_{state}_{county}_{timestamp}.json`

## Where to Add New Code

**New ingestion source (e.g. new county tax portal):**
- Primary code: `src/{county}_{domain}_api.py` (e.g. `src/blount_tax_delinquent_api.py`)
- Pattern: per-record dataclass + `fetch_*()` + `to_notice_data(rec)` + module `__main__` CLI
- Wire into orchestrator: add `_fetch_<county>()` wrapper in matching `*_pipeline.py` and add to the `if "<County>" in selected:` chain in `fetch_*()`
- Tests: `tests/test_{county}_{domain}_api.py`

**New enrichment step:**
- Primary code: `src/{name}_enricher.py` (e.g. `src/court_records_enricher.py`)
- Pattern: `enrich_<thing>(notices: list[NoticeData], <api_key>) -> None` mutating in place
- Wire into pipeline: add a new step block in `src/enrichment_pipeline.py:run_enrichment_pipeline` with `try/except ImportError` + `try/except Exception`, an `opts.skip_<name>` flag in `PipelineOptions`, and a smart-skip `opts.has_<name>` check in `detect_existing_enrichment()`
- Tests: `tests/test_{name}_enricher.py`

**New CLI mode:**
- Primary code: add a `_run_<mode>(args)` helper in `src/main.py`
- Wire in: extend the `mode` argparse `choices=[...]` list at `src/main.py:1011` and add the dispatch branch at the bottom of `cli_main()`
- Add any mode-specific args to the existing argparse block (don't introduce subparsers)
- Add credential-check rules to `src/main.py:_preflight_check`

**New notice type:**
- Add to `VALID_NOTICE_TYPES` in `src/dropbox_watcher.py:27`
- Add regex parsers in `src/notice_parser.py` (and update `parse_notice_page()` to call them)
- Add LLM prompt in `src/llm_parser.py` if structured extraction is needed
- Add list-name mapping in `src/datasift_formatter.py` (`notice_type` → DataSift list name)
- Add tag generation in `src/datasift_formatter.py:_build_tags`
- Add validation rule in `src/enrichment_pipeline.py:_validate_records` (consider adding to `_NO_PROPERTY_ADDRESS_TYPES` if PR-mailing-only)

**New output format:**
- Primary code: `src/{name}_formatter.py` (CSV-like) or `src/{name}_generator.py` (PDF/doc-like)
- Wire in: import lazily inside the relevant `_run_*` helper in `src/main.py`
- Tests: `tests/test_{name}_formatter.py`

**New external API integration:**
- Primary code: `src/{service}_{role}.py` (e.g. `src/trestle_phone_validator.py`)
- Add API key to `src/config.py` (`os.getenv` block) and `.env.example`
- Add preflight credential check in `src/main.py:_preflight_check`
- Document in `CLAUDE.md` Environment Variables section

**New analysis tool (deal analyzer-like):**
- Primary code: `src/{name}_analyzer.py`
- Should read from `output/` CSVs, not from the main pipeline
- Add a CLI mode in `src/main.py` (`comp`, `rehab`, `analyze-deal` are existing examples)

**Utilities:**
- Shared OCR helpers: `src/image_utils.py`
- Shared LLM client: `src/llm_client.py`
- Shared DataSift Playwright helpers: `src/datasift_core.py`
- Configuration constants + regexes: `src/config.py`
- Schema fields + general regex parsers: `src/notice_parser.py`

**Tests:**
- Pytest unit tests: `tests/test_<module>.py`
- One-off maintenance scripts: `tests/run_<task>.py` or `tests/export_<format>.py`
- Live integration scripts (Playwright, paid APIs): project root `test_<feature>.py`
- Ground-truth fixtures: `tests/<name>_ground_truth.json`

## Special Directories

**`output/`:**
- Purpose: Generated CSVs, JSON exports, and PDFs from every pipeline run
- Generated: Yes (auto-created by `src/config.py:34`)
- Committed: No (`.gitignore`)

**`output/reports/`:**
- Purpose: Per-record deep-prospecting PDFs from `src/report_generator.py`
- Generated: Yes (created at runtime by `_run_scrape_pipeline`)
- Committed: No

**`logs/`:**
- Purpose: Date-stamped CLI run logs
- Generated: Yes (auto-created by `src/config.py:35`, written by `setup_logging()`)
- Committed: No (`.gitignore`)

**`.actor/`:**
- Purpose: Apify Actor manifest + input schema
- Generated: No (hand-edited)
- Committed: Yes

**`.planning/codebase/`:**
- Purpose: GSD codebase mapping documents
- Generated: Yes (by `/gsd-map-codebase`)
- Committed: Per project convention (typically yes)

**`.venv/`:**
- Purpose: Local virtualenv
- Generated: Yes (developer-local)
- Committed: No

**Note on the "Skills for REI" tree (referenced in `CLAUDE.md`):**
The 13 REI skill ZIP files documented in `CLAUDE.md` (`Skills for REI/improved/*.skill`, `*.plugin`) are NOT present in this working tree. They're produced and distributed externally for DataSift community use; not part of the runnable codebase.

---

*Structure analysis: 2026-04-30*
