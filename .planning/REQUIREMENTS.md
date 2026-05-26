# Requirements: SiftStack

**Defined:** 2026-05-23
**Core Value:** Convert public-record distress signals into actionable, skip-traced, in-tier, decision-maker-attached leads inside DataSift on a low-cost, daily, hands-off cadence — at sub-cent-per-record economics that beat third-party vendors.

> SiftStack is a mature, operational platform. v1 below is split into **Validated** (the shipped backbone, captured here for traceability) and **Active v1.1** (the open backlog from CLAUDE.md "What's NOT done" notes + CONCERNS.md high-priority bugs).
> All Active requirements are mapped to phases in ROADMAP.md.

## v1 Requirements

### Validated (shipped — v1.0 backbone, locked)

These describe the operational baseline as of 2026-05-23. Captured for traceability so future phase work can reference what already exists; they are not in the active roadmap.

#### Ingestion (INGEST)

- ✓ **INGEST-01**: APN scraper for Jefferson + Madison + Marshall (foreclosure, probate, pre-probate, code-violation saved searches) — v1.0
- ✓ **INGEST-02**: Three-tier foreclosure extraction (DOM → pdfminer → pypdfium2+Tesseract OCR) with `_normalize_pdf_text()` de-hyphenation — v1.0
- ✓ **INGEST-03**: Jefferson Benchmark Web post-probate adapter (login-required case management) — v1.0
- ✓ **INGEST-04**: legacy.com Birmingham + Huntsville + Marshall County obit harvester with URL upgrade to obits.al.com — v1.0
- ✓ **INGEST-05**: Madison + Jefferson + Marshall property API adapters (AssuranceWeb + E-Ring) — v1.0
- ✓ **INGEST-06**: Madison tax-delinquent (Kendo Grid JSON) + Jefferson tax-sale (HTML tables) adapters — v1.0
- ✓ **INGEST-07**: Huntsville Unsafe Buildings PDF auto-discovery + 3-column parsing — v1.0
- ✓ **INGEST-08**: Birmingham Accela Playwright adapter (5 enforcement record types) — v1.0
- ✓ **INGEST-09**: Hoover SeeClickFix citizen-complaint adapter (lat/lng bbox) — v1.0
- ✓ **INGEST-10**: Courthouse photo pipeline (Dropbox auto-poll → OpenCV → Tesseract PSM 4 → LLM parse) for all 7 notice types (TN legacy path) — v1.0

#### Enrichment (ENR)

- ✓ **ENR-V01**: Unified 10-step enrichment pipeline with idempotent skip flags and graceful degradation — v1.0
- ✓ **ENR-V02**: Probate property locator with Tier 1 (decedent) + Tier 2 (PR) waterfall — v1.0
- ✓ **ENR-V03**: Multi-parcel decedent return with primary-residence ranking — v1.0
- ✓ **ENR-V04**: Smarty USPS standardization + ZIP+4 + geocoding + RDI/vacancy detection — v1.0
- ✓ **ENR-V05**: Zillow enrichment via OpenWebNinja (Zestimate, MLS, equity, beds/baths/sqft) — v1.0
- ✓ **ENR-V06**: Obituary enricher with single-call family-graph LLM extraction + Ancestry/Newspapers fallback + DOD sanity check (3yr) — v1.0
- ✓ **ENR-V07**: AL intestate-succession decision-maker ranking (executor > spouse > children > siblings) — v1.0
- ✓ **ENR-V08**: Tracerfy batch skip-trace with heir-phone promotion to NoticeData phone slots — v1.0
- ✓ **ENR-V09**: Trestle phone validator with 5-tier dial priority (81-100 → 0-20) — v1.0
- ✓ **ENR-V10**: Entity researcher (LLM + web search) for LLC/Corp/Trust resolution — v1.0
- ✓ **ENR-V11**: Pre-probate three-path property search waterfall (decedent name → obit-stated address → spouse name) — verified +16pp recall lift to ~92% — v1.0
- ✓ **ENR-V12**: Tier 1/Tier 2 ZIP gate codified in `src/target_zips.py` for all 3 counties — v1.0
- ✓ **ENR-V13**: Smarty ZIP recovery for AssuranceWeb-platform counties (`_smarty_zip_for_assuranceweb_address`) — v1.0

#### Pipelines / Orchestrators (PIPE)

- ✓ **PIPE-01**: Benchmark Jefferson post-probate orchestrator (`benchmark_pipeline_al.py`) — v1.0
- ✓ **PIPE-02**: APN post-probate orchestrator (`apn_probate_pipeline_al.py`, Jefferson + Madison + Marshall) — v1.0
- ✓ **PIPE-03**: Pre-probate orchestrator (`pre_probate_pipeline_al.py`, all 3 counties with cross-county routing) — v1.0
- ✓ **PIPE-04**: Tax-distress orchestrator (`tax_distress_pipeline.py`) with auction-date stamping via `next_al_tax_sale_date()` — v1.0
- ✓ **PIPE-05**: Distress-proxy orchestrator (`distress_proxy_pipeline.py`) as Huntsville code-enforcement stopgap — v1.0
- ✓ **PIPE-06**: Code-violation orchestrator (`code_violation_pipeline.py`) wiring Huntsville + Birmingham + Hoover — v1.0
- ✓ **PIPE-07**: Marshall county coverage (probate + pre-probate + foreclosure + APN-floor code-violation live; tax-delinquent stub auto-activates on county re-enable) — v1.0

#### Output / CRM (OUT)

- ✓ **OUT-01**: DataSift 80-column CSV formatter with 15 SiftStack custom + 7 AL probate enrichment fields — v1.0
- ✓ **OUT-02**: DataSift Playwright login + Add Data wizard (existing-list mode) — v1.0
- ✓ **OUT-03**: Per-distressor split upload (`upload_to_datasift_per_distressor`) routing to mapped list names — v1.0
- ✓ **OUT-04**: DataSift post-upload Enrich Property Information + Skip Trace automation — v1.0
- ✓ **OUT-05**: DataSift preset management (12 niche + 9 bulk preset sold-exclusion) — v1.0
- ✓ **OUT-06**: DataSift Sequence Builder (drag trigger + condition + action chain) + Sold Property Cleanup sequence — v1.0
- ✓ **OUT-07**: DataSift SiftMap sold-property tagging workflow — v1.0
- ✓ **OUT-08**: Sift output CSV writer (80+ columns) with split-mode per county+type — v1.0
- ✓ **OUT-09**: Per-record deep-prospecting PDFs via reportlab — v1.0
- ✓ **OUT-10**: Slack/Discord webhook daily notifications with per-pipeline action cards — v1.0
- ✓ **OUT-11**: Google Drive upload via service account (Apify-mode) — v1.0
- ✓ **OUT-12**: Excel exporter (7-sheet Knox-style market reports) — v1.0

#### Analysis Layer (ANL)

- ✓ **ANL-01**: Comp analyzer (Two-Bucket ARV, disclosure routing) — v1.0
- ✓ **ANL-02**: Rehab estimator (4-tier room-by-room) — v1.0
- ✓ **ANL-03**: Deal analyzer (MAO + financing scenarios + exit strategy) — v1.0
- ✓ **ANL-04**: Market analyzer (6-factor zip code scoring with grades A/B/C/D) — v1.0
- ✓ **ANL-05**: Buyer prospector (cash buyer list builder, 50-state SOS support) — v1.0
- ✓ **ANL-06**: Deep prospector (4-level research depth coordinator) — v1.0
- ✓ **ANL-07**: Lead manager (4 Pillars of Motivation + STABM) — v1.0
- ✓ **ANL-08**: Niche sequential (12 niche + 9 bulk marketing preset compositions) — v1.0
- ✓ **ANL-09**: Sequence templates (26 TCA sequence definitions across 5 folders) — v1.0
- ✓ **ANL-10**: Playbook generator (SOP + script + checklist generator from transcripts) — v1.0
- ✓ **ANL-11**: Market Finder extractor (Playwright; ZIP + neighborhood pagination) — v1.0

#### Deployment / Infrastructure (DEP)

- ✓ **DEP-01**: Apify Actor deployment (`apify/actor-python-playwright:3.12` base; Dataset + KVS output) — v1.0
- ✓ **DEP-02**: Apify Console input schema with 24 fields (mode, county filters, all API keys, pipeline toggles) — v1.0
- ✓ **DEP-03**: Per-CLI mode dispatch in `main.py` (15+ modes: daily, historical, photo-import, dropbox-watch, manage-presets, manage-sold, etc.) — v1.0
- ✓ **DEP-04**: REI Skill Library (13 distribution-ready `.skill` / `.plugin` ZIPs at `learn.datasift.ai/claude-skills-rei`) — v1.0

### Active (v1.1 — open backlog from CLAUDE.md + CONCERNS.md)

These are mapped to phases in ROADMAP.md.

#### Operations / Scheduler (OPS)

- [ ] **OPS-01**: Unified daily scheduler across all 5 pipelines (post-probate, APN post-probate, pre-probate, tax-distress, code-violation) + Marshall coverage, replacing per-pipeline manual invocation
- [ ] **OPS-02**: Single consolidated daily Slack post that unifies all DataSift leads across pipelines (currently per-pipeline)
- [ ] **OPS-03**: Full pipeline funnel transparency on every run — drop counts at every gate (scraped → ZIP-gated → property-matched → obit-matched → tracerfy-matched → uploaded), surfaced in Slack + terminal
- [ ] **OBS-01**: Per-service success-rate metric on Slack daily report (2Captcha solve rate, Smarty hit rate, Tracerfy match rate, LLM extraction success rate) so early-warning surfaces before failed runs

#### Coverage Gap Closure (COV)

- [ ] **PROB-MAD-01**: Madison post-probate Bonds-as-LT-proxy adapter via `madisonprobate.countygovservices.com` Probate Bonds category (~3-4hr build; catches non-publishing PRs that APN misses)
- [ ] **PREPROB-01**: Full-text partner-site fetch for pre-probate funeral-home pages (Welch, Larkin & Scott, dignitymemorial, etc.) — recovers family graph for ~30-40% of obits that still drop after legacy → obits.al.com upgrade (~2hr fix)
- [ ] **ENR-01**: Vacancy enrichment for inherited probate properties (USPS NCOA + water-shutoff signals) to tighten lead score with "actually unoccupied" confirmation
- [ ] **CODE-MAD-01**: Huntsville FOIA-driven soft-violation adapter (`huntsville_code_violations_api.py`) parallel to Birmingham Accela, wired into `code_violation_pipeline._fetch_madison()` once monthly export is flowing
- [ ] **TAX-MAR-01**: Marshall tax-delinquent parser back-fill (replace `NotImplementedError` stub when Marshall County re-enables AssuranceWeb DelinquentParcels listing)

#### Verification / Quality (QUAL)

- [ ] **PREPROB-02**: Ground-truth precision check of pre-probate output against `REISift_Upload_Jefferson_Pre-Probate.csv` (2,438-row RealSupermarket April 2026 dump) — recall is measured (~92%), precision is not

#### Critical Bug Fixes (BUGFIX)

- [ ] **BUGFIX-01**: Madison `_search_madison` name-format bug — passes `(parts[0], parts[1:])` querying "FIRST" as last name; add Jefferson-style last-first retry. Madison probate property-locator hit rate is silently degraded
- [ ] **BUGFIX-02**: Apify Actor cold-start `AttributeError` on dead `config.TNPN_EMAIL` / `config.TNPN_PASSWORD` at `src/main.py:184` — every scheduled Apify run currently crashes before scraping. Plus update Actor `_cred_map` to drop `tn_username`/`tn_password`
- [ ] **BUGFIX-03**: `_GARBAGE_RE` regex mismatch with its docstring (currently matches non-alphanumeric, should match non-letter). Numeric-only OCR'd addresses pass validation and ship to DataSift as garbage. Change to `r"^[^a-zA-Z]*$"`

#### Parser Hardening (PARSER)

- [ ] **PARSER-01**: Add `PR_ADDRESS_NAME_FIRST_RE` (`<name>\n<title>\n<address>`) to `notice_parser._parse_pr_address()` — AL signature-block format is name-before-title; current regex requires title-before-address. AL probate PR mailing addresses systematically missing when LLM fallback unavailable

## v2 Requirements

Deferred to future milestone. Tracked but not in current v1.1 roadmap.

### Tech Debt / Refactor

- **REFAC-01**: Define `CountyPropertyAdapter` Protocol + registry to eliminate `if county == "..."` branching in `probate_property_locator.py` and across orchestrators. Each new county becomes a single adapter registration
- **REFAC-02**: Consolidate 4 name-splitter implementations (`data_formatter._split_name`, `datasift_formatter._split_name`, `notice_parser._split_full_name`, `tracerfy_skip_tracer._split_name`) onto the most complete (`_split_full_name`)
- **REFAC-03**: Split `src/datasift_uploader.py` (4,246 lines) into per-workflow modules: `datasift_login.py`, `datasift_upload.py`, `datasift_presets.py`, `datasift_sequences.py`, `datasift_siftmap.py`
- **REFAC-04**: Factor `run_full_pipeline(notices, opts, on_event)` shared helper so `actor_main()` and `cli_main()` stop drifting (root cause of BUGFIX-02)
- **REFAC-05**: Mode registry pattern for `main.py` so each mode is a class with `add_args(parser)` + `run(args)` and `main.py` discovers them
- **REFAC-06**: Add `tesseract-ocr` to Dockerfile so `photo-import` / `pdf-import` modes work on Apify (future-trap fix)
- **REFAC-07**: Generate `requirements.lock` via pip-compile or migrate to uv/poetry; pin upper bounds on Apify, dropbox, anthropic, playwright, ddgs

### Test Suite Reorganization

- **TEST-01**: Move repo-root `test_*.py` integration scripts into `tests/integration/` behind a `requires_credentials` pytest marker; move `tests/run_*.py` and `tests/export_*.py` maintenance scripts out of `tests/` into `scripts/`
- **TEST-02**: Add `conftest.py` with `--run-live` flag to gate credentialed tests
- **TEST-03**: Unit-test coverage for AL probate property locator + Madison/Jefferson/Marshall property APIs + Birmingham Accela scraper + Apify cold-start path (each ~5-line golden tests would catch the existing bug class)

### Observability

- **OBS-V2-01**: Per-county / per-pipeline daily dashboards (HTML or Apify metrics) showing 7-day rolling enriched-lead yield, ZIP-gate survival rate, and skip-trace hit rate
- **OBS-V2-02**: Cost-per-lead tracking (2Captcha + LLM + Smarty + Tracerfy spend per enriched record)

### New Coverage

- **COV-V2-01**: Additional AL counties (Shelby, Lee, Tuscaloosa, Mobile, Baldwin) — each requires per-county tier ZIP analysis, property-API adapter, foreclosure/probate SAVED_SEARCHES, optional code-enforcement scraper
- **COV-V2-02**: Eviction + divorce notice types via APN (currently photo-import-only)
- **COV-V2-03**: Cash-buyer + portfolio analysis surfaced as a daily lead source (currently analysis-layer batch only)
- **COV-V2-04**: Vendor-comparison precision metric for tax-distress + code-violation pipelines (parallel to PREPROB-02 for pre-probate)

### Security / Hygiene

- **SEC-V2-01**: Ship missing intermediate CA cert as bundled `cacert.pem` + pass `verify="path/to/bundle.pem"` instead of `verify=False` in `jefferson_property_api.py` + `jefferson_tax_delinquent_api.py`
- **SEC-V2-02**: Convert blanket `except Exception: pass` patterns (39 in datasift_uploader, 21 in main, 19 in obituary_enricher, 12 in ancestry_enricher) to `except Exception as e: logger.debug(...)` for observability
- **SEC-V2-03**: Remove stale TN-era code paths (TN courthouse photo pipeline if Knox/Blount production is permanently retired) OR formally extend photo LLM prompts to AL courthouse layout

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Vestavia / Trussville / Hueytown / Pinson municipal code-enforcement scrapers | Researched 2026-05-08 — all 4 confirmed not publicly scrapeable (Connect/OpenGov permits-only, Freshdesk auth-gated, govtportal payments-only, no infra). FOIA is only path, parallel to Huntsville |
| Marshall code-enforcement online scraper | Researched 2026-05-12 — no Accela/SeeClickFix/municipal portal. APN-floor scraper provides coverage |
| Real-time DataSift REST API integration | DataSift exposes NO REST API. Playwright UI is the only path |
| DataSift "Update Data" wizard mode | Different downstream sequence than "Add Data"; modal interrupts file-input. Canonical helper stays on Add Data + existing-list |
| Multi-instance concurrent local CLI runs | State files single-machine. Production is Apify KVS; local CLI is single-developer |
| TN-county (Knox/Blount) production daily scraping | README framing is historical/aspirational; live deployment is AL-only. TN code preserved but not maintained |
| Renaming existing `data_formatter.SIFT_COLUMNS` / `datasift_formatter._build_row()` columns | Apify dataset views + DataSift custom-field mappings + analysis layer all depend on names. Append-only |
| Hardcoded annual auction dates | Use `next_al_tax_sale_date()` dynamic computation |
| Tax-roll address fallback regex on raw notice body | Pulls courthouse / auction-location / trustee-office addresses. Only extract after `_PROP_INDICATOR` phrases |
| Tesseract OSD (`fix_rotation()`) on phone photos | OSD often fails on terminal-screen images; 270° fallback rotates correct images sideways. EXIF transpose only for phones; OSD for PDFs |
| Standard CLAHE + adaptive threshold for terminal-screen OCR | Moire pattern defeats adaptive thresholding. Use bilateral filter (`cv2.bilateralFilter(gray, 15, 75, 75)`) + Otsu, PSM 4 not PSM 6 |
| Running obituary search on probate PR/executor name | PR is alive; obituary belongs to decedent. Triggers wrong-person matches. Use "probate preset" — set DM = named PR directly, skip obituary |
| Obituary acceptance without DOD sanity check | Common names match decades-old deaths. Reject DOD > 3 years before notice filing (`MAX_DOD_GAP_YEARS = 3`) |
| Single-keyword code-violation searches (CONDEMNATION / DEMOLITION alone) | 100% false-positive rate (drug forfeitures, ALDOT eminent-domain, construction bids). AND-combine with action verbs (`DEMOLITION + UNSAFE STRUCTURE`, `CONDEMNED STRUCTURE`) |
| Real-time chat / video / mobile-app surfaces | SiftStack is a backend operations platform. CRM UI is DataSift's responsibility; no native UI in scope |

## Traceability

Active v1.1 requirements mapped to phases.

| Requirement | Phase | Status |
|-------------|-------|--------|
| BUGFIX-01 | Phase 1 | Pending |
| BUGFIX-02 | Phase 1 | Pending |
| BUGFIX-03 | Phase 1 | Pending |
| PARSER-01 | Phase 1 | Pending |
| OPS-03 | Phase 2 | Pending |
| OBS-01 | Phase 2 | Pending |
| OPS-01 | Phase 3 | Pending |
| OPS-02 | Phase 3 | Pending |
| PROB-MAD-01 | Phase 4 | Pending |
| PREPROB-01 | Phase 4 | Pending |
| CODE-MAD-01 | Phase 4 | Pending |
| TAX-MAR-01 | Phase 4 | Pending |
| ENR-01 | Phase 5 | Pending |
| PREPROB-02 | Phase 5 | Pending |

**Coverage:**
- v1.1 Active requirements: 14 total
- Mapped to phases: 14
- Unmapped: 0 ✓

(Validated v1.0 requirements are not mapped to active phases — they describe shipped capability for traceability only.)

---
*Requirements defined: 2026-05-23*
*Last updated: 2026-05-23 after doc-ingest bootstrap*
