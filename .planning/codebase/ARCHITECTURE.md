<!-- refreshed: 2026-04-30 -->
# Architecture

**Analysis Date:** 2026-04-30

## System Overview

SiftStack is a **pipeline-oriented data platform** with multiple independent ingestion paths converging on a single canonical schema (`NoticeData`), then flowing through a unified enrichment chain into the DataSift CRM and downstream consumers.

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER (7+ paths)                          │
├──────────────┬──────────────┬──────────────┬──────────────┬────────────────┤
│  Web Scrape  │  PDF Import  │ Photo Import │ Dropbox Poll │ County API     │
│ APN Site     │ Tax Sale PDF │ Courthouse   │ Auto Watcher │ Adapters       │
│ `scraper.py` │ `pdf_         │ Terminals    │ `dropbox_    │ (Madison/      │
│              │ importer.py` │ `photo_      │ watcher.py`  │ Jefferson/     │
│              │              │ importer.py` │              │ Huntsville/    │
│              │              │              │              │ Birmingham)    │
└──────┬───────┴──────┬───────┴──────┬───────┴──────┬───────┴───────┬────────┘
       │              │              │              │               │
       │              │              │              │               │
       ▼              ▼              ▼              ▼               ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                       ORCHESTRATORS (per-domain)                            │
│                                                                             │
│   `tax_distress_pipeline.py`     `code_violation_pipeline.py`              │
│   (Madison + Jefferson tax)      (Huntsville + Birmingham code)            │
│                                                                             │
│   `extract_market_finder.py`     `scraper.py + main.py`                    │
│   (DataSift Market Finder)       (foreclosure + probate web scrape)        │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│              CANONICAL SCHEMA — `NoticeData` dataclass                      │
│              `src/notice_parser.py` (dataclass, ~170 fields)                │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│           UNIFIED ENRICHMENT PIPELINE (10 sequential steps)                 │
│                  `src/enrichment_pipeline.py`                               │
│                                                                             │
│   Filter Sold → Dedup → Vacant Filter → Entity Research → Entity Filter →  │
│   Probate Property Lookup → Parcel Lookup → Tax Delinquency → Smarty →     │
│   Commercial Filter → Reverse Geocode → Zillow → Obituary → Validate →     │
│   Mailable Flag → Summary                                                   │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌──────────────────┐   ┌──────────────────────┐   ┌────────────────────────┐
│  Tracerfy        │   │  Trestle Phone       │   │  Report Generator      │
│  Skip Trace      │   │  Validation          │   │  (PDFs for DP)         │
│  `tracerfy_      │   │  `phone_validator.py`│   │  `report_generator.py` │
│  skip_tracer.py` │   │                      │   │                        │
└────────┬─────────┘   └──────────┬───────────┘   └─────────┬──────────────┘
         │                        │                         │
         └────────────────────────┴─────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                       OUTPUT / SINK LAYER                                   │
├────────────────┬─────────────────┬──────────────────┬──────────────────────┤
│  Sift CSV      │  DataSift CSV   │  DataSift CRM    │  Slack / Drive       │
│  `data_        │  `datasift_     │  `datasift_      │  `slack_notifier.py` │
│  formatter.py` │  formatter.py`  │  uploader.py`    │  `drive_uploader.py` │
│  (output/)     │  (80 cols)      │  (Playwright)    │                      │
└────────────────┴─────────────────┴──────────────────┴──────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| CLI entry point | Argparse mode dispatch (15+ modes) | `src/main.py` |
| Apify Actor entry | Cloud-deployment async orchestrator | `src/main.py` (`actor_main()`) + `src/__main__.py` |
| Configuration | Env vars, selectors, SAVED_SEARCHES, regexes, paths | `src/config.py` |
| Canonical schema | `NoticeData` dataclass + parsers | `src/notice_parser.py` |
| APN Web scraper | Playwright + ASP.NET ViewState navigation | `src/scraper.py` |
| CAPTCHA solver | 2Captcha API integration for reCAPTCHA v2 | `src/captcha_solver.py` |
| Foreclosure filter | Drops non-trustee-sale notices | `src/foreclosure_filter.py` |
| LLM fallback parser | Claude Haiku for regex misses | `src/llm_parser.py` + `src/llm_client.py` |
| PDF import | pypdfium2 + Tesseract OCR for scanned tax sale PDFs | `src/pdf_importer.py` |
| Photo import | OpenCV preprocessing + Tesseract OCR for terminal photos | `src/photo_importer.py` |
| Dropbox watcher | Cursor-based polling, folder-path → county/type resolution | `src/dropbox_watcher.py` |
| OCR utilities | Shared Tesseract + image rotation helpers | `src/image_utils.py` |
| Unified enrichment | 10-step canonical pipeline orchestrator | `src/enrichment_pipeline.py` |
| Smarty enricher | USPS address standardization + geocode | `src/address_standardizer.py` |
| Zillow enricher | Property data via OpenWeb Ninja API | `src/property_enricher.py` |
| Tax enricher | Knox Tax API + parcel address lookup | `src/tax_enricher.py` |
| Obituary enricher | Deceased detection + heir/DM extraction (largest module: 3030 lines) | `src/obituary_enricher.py` |
| Ancestry SSDI | Ancestry.com SSDI lookup via Playwright | `src/ancestry_enricher.py` |
| Tracerfy skip trace | Phone + email batch lookup | `src/tracerfy_skip_tracer.py` |
| Trestle phone scoring | Phone tier classification (5 tiers) | `src/phone_validator.py` |
| Entity researcher | LLC/Corp → person extraction (LLM + web search) | `src/entity_researcher.py` |
| Probate property locator | Multi-tier name → property lookup (Jefferson + Madison) | `src/probate_property_locator.py` |
| Property lookup (Knox) | Knox Tax API name search for probate | `src/property_lookup.py` |
| Madison county adapter | AssuranceWeb owner + situs-address search | `src/madison_property_api.py` |
| Jefferson county adapter | E-Ring Capture API owner + situs-address search | `src/jefferson_property_api.py` |
| Madison tax delinquent | Bulk tax-delinquent feed | `src/madison_tax_delinquent_api.py` |
| Jefferson tax delinquent | Bulk tax-sale roster (Birmingham + Bessemer HTML tables) | `src/jefferson_tax_delinquent_api.py` |
| Huntsville unsafe buildings | Monthly PDF list + auto-discovery | `src/huntsville_unsafe_buildings_api.py` |
| Birmingham code enforcement | Accela Citizen Access Playwright scraper (5 record types) | `src/birmingham_code_enforcement_api.py` |
| Tax distress orchestrator | Unified Madison + Jefferson tax pipeline | `src/tax_distress_pipeline.py` |
| Code violation orchestrator | Unified Huntsville + Birmingham code pipeline | `src/code_violation_pipeline.py` |
| DataSift formatter | NoticeData → 80-col DataSift CSV + tag generation | `src/datasift_formatter.py` |
| DataSift uploader | Playwright wizard upload + enrich + skip trace + presets + sequences (4246 lines) | `src/datasift_uploader.py` |
| DataSift core | Shared login + cookie + popup-dismissal helpers | `src/datasift_core.py` |
| Sift CSV formatter | Output CSV with 80+ enrichment columns | `src/data_formatter.py` |
| Market Finder extractor | Playwright extraction of state/county data | `src/extract_market_finder.py` |
| Market analyzer | 6-factor zip code scoring + grading | `src/market_analyzer.py` |
| Comp analyzer | Two-Bucket ARV with disclosure routing | `src/comp_analyzer.py` |
| Rehab estimator | 4-tier room-by-room cost estimation | `src/rehab_estimator.py` |
| Deal analyzer | MAO + financing scenarios + exit strategy | `src/deal_analyzer.py` |
| Buyer prospector | Cash buyer list builder | `src/buyer_prospector.py` |
| Deep prospector | 4-level research depth coordinator | `src/deep_prospector.py` |
| Lead manager | 4 Pillars qualification + STABM | `src/lead_manager.py` |
| Niche sequential | 12 + 9 marketing preset compositions | `src/niche_sequential.py` |
| Sequence templates | 26 TCA sequence definitions | `src/sequence_templates.py` |
| Playbook generator | SOP + script + checklist generator | `src/playbook_generator.py` |
| Report generator | Per-record PDF deep-prospecting reports (reportlab) | `src/report_generator.py` |
| Excel exporter | 7-sheet Knox market reports | `src/excel_exporter.py` |
| Case summary | Case context blocks for PDFs | `src/case_summary.py` |
| Slack notifier | Run-summary + error webhook posts | `src/slack_notifier.py` |
| Drive uploader | Google Drive service-account upload | `src/drive_uploader.py` |
| Dropbox uploader | Dropbox API upload + share-link generation | `src/dropbox_uploader.py` |

## Pattern Overview

**Overall:** Pipeline-oriented data flow with **fan-in ingestion → unified canonical schema → linear enrichment chain → fan-out sinks**.

**Key Characteristics:**
- **Single canonical schema** — every ingestion path emits `NoticeData` (defined once in `src/notice_parser.py`); all downstream code depends only on this dataclass.
- **Idempotent step skipping** — every enrichment step in `src/enrichment_pipeline.py` checks both `skip_*` flags AND `has_*` detection flags so CSV re-imports don't redo expensive work.
- **Graceful degradation** — every external API call is wrapped in `try/except` that logs a warning and continues; missing API keys downgrade specific steps but never fail the pipeline.
- **Two deployment targets** — same code runs as local CLI (`python src/main.py <mode>`) or Apify Actor (`python -m src` → `actor_main()`); `APIFY_IS_AT_HOME` env var distinguishes.
- **Playwright + ASP.NET WebForms-aware** — APN, Birmingham Accela, and DataSift are all ASP.NET / styled-component SPAs requiring postback navigation, not simple HTTP.
- **Per-domain orchestrators** — `tax_distress_pipeline.py` and `code_violation_pipeline.py` each chain two county adapters into one `fetch_*()` function; called by both CLI mode and downstream consumers.

## Layers

**Ingestion Layer:**
- Purpose: Acquire raw notices from external sources and produce `NoticeData` lists
- Location: `src/scraper.py`, `src/pdf_importer.py`, `src/photo_importer.py`, `src/dropbox_watcher.py`, `src/*_api.py` (county adapters), `src/extract_market_finder.py`
- Contains: Playwright automation, OCR + LLM parsing, county-specific HTTP adapters
- Depends on: `notice_parser.NoticeData` schema, `image_utils`, `llm_parser`, `captcha_solver`
- Used by: Orchestrators (`*_pipeline.py`) and `main.py` mode dispatchers

**Orchestrator Layer:**
- Purpose: Combine multiple county adapters and source-specific transforms into single fetch entry points
- Location: `src/tax_distress_pipeline.py`, `src/code_violation_pipeline.py`, `src/main.py` (`_run_*` helpers)
- Contains: Per-domain fetch functions, auction-date stamping, `to_notice_data()` converters
- Depends on: County adapters (`*_api.py`)
- Used by: `main.py` CLI mode dispatch and external scripts

**Schema Layer:**
- Purpose: Define the canonical `NoticeData` dataclass and primary regex parsers
- Location: `src/notice_parser.py`
- Contains: `NoticeData` dataclass (~170 fields), regex patterns for foreclosure/probate/eviction/code-violation, `parse_notice_page()`, `is_target_county()`
- Depends on: Playwright (for `parse_notice_page` async helper)
- Used by: Every other module in the codebase

**Enrichment Layer:**
- Purpose: Add USPS-confirmed address, property data, tax status, deceased detection, decision-maker, phones to each `NoticeData`
- Location: `src/enrichment_pipeline.py` (orchestrator) + `src/*_enricher.py` and supporting modules
- Contains: 10-step linear pipeline, `PipelineOptions` dataclass, `detect_existing_enrichment()` smart-skip detection
- Depends on: All `*_enricher.py`, `tax_enricher.py`, `address_standardizer.py`, `property_enricher.py`, `entity_researcher.py`, `probate_property_locator.py`
- Used by: All ingestion paths after they produce `NoticeData`

**Output Layer:**
- Purpose: Format enriched records into Sift CSV, DataSift CSV, PDFs, and push to CRM/Slack/Drive
- Location: `src/data_formatter.py`, `src/datasift_formatter.py`, `src/datasift_uploader.py`, `src/report_generator.py`, `src/slack_notifier.py`, `src/drive_uploader.py`, `src/dropbox_uploader.py`
- Contains: CSV column definitions (Sift 80 cols, DataSift 80 cols), tag-builder logic, Playwright wizard automation, Slack webhook payload builders
- Depends on: `notice_parser.NoticeData`
- Used by: `main.py` mode dispatchers and Apify Actor

**Analysis Layer (parallel — not in main pipeline):**
- Purpose: Standalone deal analysis tools invoked via separate CLI modes
- Location: `src/comp_analyzer.py`, `src/rehab_estimator.py`, `src/deal_analyzer.py`, `src/market_analyzer.py`, `src/buyer_prospector.py`
- Contains: ARV computation, rehab cost rules, MAO formulas, zip-code scoring weights
- Depends on: Output CSVs from main pipeline (reads from `output/`)
- Used by: `main.py` modes `comp`, `rehab`, `analyze-deal`, `market-analysis`, `buyer-prospect`

## Data Flow

### Primary Request Path — Daily Web Scrape

1. CLI dispatch (`src/main.py:1005` `cli_main()` or `src/main.py:126` `actor_main()`) parses mode + filters
2. Preflight check (`src/main.py:52` `_preflight_check`) verifies API keys, 2Captcha balance, APN reachability
3. Filter SAVED_SEARCHES by `--counties`/`--types` (`src/main.py:31` `_filter_searches`)
4. Async scrape (`src/scraper.py:scrape_all`) — submit form → paginate results → click each notice → CAPTCHA solve → `parse_notice_page` → emit `NoticeData`
5. Async probate property lookup if probate notices lack address (`src/property_lookup.py:lookup_decedent_properties`)
6. Run unified enrichment pipeline (`src/enrichment_pipeline.py:run_enrichment_pipeline`) — 10 sequential steps
7. Tracerfy batch skip trace (`src/tracerfy_skip_tracer.py:batch_skip_trace`) — phones + emails for DP candidates
8. Trestle phone scoring (`src/phone_validator.py:score_record_phones`) — assigns 5-tier dial priority
9. Generate per-record PDFs for deep-prospecting candidates (`src/report_generator.py:generate_record_pdf`)
10. Write Sift CSV (`src/data_formatter.py:write_csv`) → `output/`
11. Write DataSift CSV (`src/datasift_formatter.py:write_datasift_split_csvs`) → `output/`
12. (Optional) Upload to DataSift via Playwright (`src/datasift_uploader.py:upload_to_datasift`)
13. (Optional) Slack notification (`src/slack_notifier.py:send_slack_notification`)
14. (Apify only) Push records to Apify dataset, save CSVs to KVS, upload to Google Drive

### PDF Import Flow

1. CLI mode `pdf-import` (`src/main.py:590` `_run_pdf_import`)
2. `src/pdf_importer.py:process_pdf` — pypdfium2 render → `image_utils.fix_rotation` → `image_utils.ocr_page` (Tesseract PSM 3) → LLM table parse (Claude Haiku) → list of `NoticeData`
3. Run unified enrichment pipeline (same as web scrape)
4. Write CSV → `output/`

### Photo Import Flow

1. CLI mode `photo-import` (`src/main.py:658` `_run_photo_import`)
2. `src/photo_importer.py:process_photos` — EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold → `image_utils.ocr_page` (Tesseract PSM 4) → `src/llm_parser.py` (Claude Haiku) → list of `NoticeData`
3. Run unified enrichment pipeline (skips vacant-land filter for `probate`/`divorce` types)
4. Write CSV → `output/`

### Dropbox Auto-Poll Flow

1. CLI mode `dropbox-watch` (loops `src/dropbox_watcher.py`)
2. `src/dropbox_watcher.py` cursor-based polling → download new photos → resolve county + notice_type from path `/{root}/{county}/{notice_type}/photo.jpg`
3. Per-batch: invoke photo import flow (above)
4. Delete from Dropbox after successful processing
5. Persist cursor + processed-file state to `dropbox_state.json` + `photo_state.json`

### Tax Distress Flow

1. `src/tax_distress_pipeline.py:fetch_tax_distress` (CLI: `python src/tax_distress_pipeline.py`)
2. Fan out to `src/madison_tax_delinquent_api.py:fetch_delinquent_parcels` and `src/jefferson_tax_delinquent_api.py:fetch_delinquent_parcels`
3. Convert each adapter's record dataclass to `NoticeData` via `to_notice_data()`
4. `apply_auction_dates(notices)` stamps next first-Tuesday-of-May on `tax_sale`-typed records
5. Optional CSV writers (`data_formatter.write_csv` and/or `datasift_formatter.write_datasift_csv`)

### Code Violation Flow

1. `src/code_violation_pipeline.py:fetch_code_violations` (CLI: `python src/code_violation_pipeline.py`)
2. Fan out to `src/huntsville_unsafe_buildings_api.py:fetch_unsafe_buildings` and `src/birmingham_code_enforcement_api.py:fetch_enforcement_cases`
3. Convert to `NoticeData`; optional `enrich_owner` flag invokes Madison/Jefferson property API address-search
4. Optional CSV writers

### 10-Step Enrichment Chain (canonical sequence in `src/enrichment_pipeline.py:run_enrichment_pipeline`)

| Step | Module | Function | Purpose |
|------|--------|----------|---------|
| 1 | `src/data_formatter.py` | `filter_sold` | Drop records flagged Sold (only when `skip_filter_sold=False`) |
| 2 | `src/data_formatter.py` | `deduplicate` | Address-based dedup (keeps most recent) |
| 3 | `src/enrichment_pipeline.py` | `_filter_vacant_land` | Drop vacant lots (no real house number) — exempt for probate/divorce |
| 3a | `src/entity_researcher.py` | `enrich_entity_data` | LLM + web search for person behind LLC/Corp/Trust |
| 3b | `src/enrichment_pipeline.py` | `_filter_entity_owners` | Drop business-entity-owned records (keep personal trusts/estates) |
| 3c | `src/tax_enricher.py` | `_probate_property_lookup` | Knox Tax API name search for probate decedents without address |
| 4 | `src/tax_enricher.py` | `lookup_parcel_addresses` | Knox parcel-ID → address resolution |
| 5 | `src/tax_enricher.py` | `enrich_tax_delinquency` | Knox Tax API delinquency status |
| 6 | `src/address_standardizer.py` | `standardize_addresses` | Smarty USPS validation + geocode |
| 6a | `src/enrichment_pipeline.py` | `_filter_commercial` | Drop Smarty-classified commercial properties |
| 7 | `src/address_standardizer.py` | `retry_with_geocoded_city` | Reverse-geocode + Smarty retry for failed lookups |
| 8 | `src/property_enricher.py` | `enrich_properties` | Zillow data via OpenWeb Ninja (Zestimate, MLS, beds/baths/sqft) |
| 9 | `src/obituary_enricher.py` | `enrich_obituary_data` | Obituary search → DOD, heirs, decision maker; optional Ancestry SSDI; optional Tracerfy tier-1 DM address |
| 9b | `src/enrichment_pipeline.py` | `_validate_records` | Reject records missing address+city+zip OR (probate/divorce) PR mailing address |
| 10 | `src/enrichment_pipeline.py` | `_compute_mailable` | Set `mailable="yes"` when address+city+zip all present |
| 11 | `src/enrichment_pipeline.py` | `_log_summary` | Per-type/county breakdown + enrichment hit rates |

**State Management:**
- Web scrape: `last_run.json` (mode date cutoff) + `seen_ids.json` (cross-run dedup, 90-day prune) + `cookies.json` (APN session)
- Dropbox: `dropbox_state.json` (cursor) + `photo_state.json` (processed-file dedup)
- CAPTCHA failures: `captcha_failed_ids.json` (notice IDs that exhausted retries; 14-day prune)
- Apify: `last_run_date` + `seen_notice_ids` keys in Actor key-value store

## Key Abstractions

**`NoticeData` dataclass:**
- Purpose: Canonical schema for every notice record across all 7 ingestion paths and 7 notice types
- Location: `src/notice_parser.py:28`
- Pattern: Dataclass with ~170 string fields (default `""`) so any subset of the pipeline can populate any subset of fields without optional-handling

**`PipelineOptions` dataclass:**
- Purpose: Configures which enrichment steps run for a given pipeline invocation
- Location: `src/enrichment_pipeline.py:30`
- Pattern: Dataclass of skip flags + sub-options + smart-detection flags; passed through `run_enrichment_pipeline()`

**`SearchConfig` (alias `SavedSearch`):**
- Purpose: Defines a keyword search against alabamapublicnotices.com
- Location: `src/config.py:108`
- Pattern: Dataclass with `(county, notice_type, search_terms, search_type, exclude_terms, days_back, notice_subtype)`

**Adapter pattern for county data:**
- Purpose: Each county's tax/property/code data has a different upstream format; adapters wrap it into a uniform per-county dataclass + `to_notice_data()` converter
- Pattern: `*PropertyRecord` / `*DelinquentRecord` / `*EnforcementRecord` dataclasses with `is_individual_owner`, `is_high_exposure` derived flags; module-level `fetch_*()` and `to_notice_data()` functions
- Examples: `src/madison_property_api.py`, `src/jefferson_property_api.py`, `src/madison_tax_delinquent_api.py`, `src/jefferson_tax_delinquent_api.py`, `src/huntsville_unsafe_buildings_api.py`, `src/birmingham_code_enforcement_api.py`

**Pipeline orchestrator pattern:**
- Purpose: Chain multiple county adapters into one entry point with shared filters and CLI
- Pattern: `_fetch_<county>()` private wrappers + public `fetch_<domain>()` returning combined `NoticeData` list; CLI via `argparse` + `_summarize()` + `_main(argv)`
- Examples: `src/tax_distress_pipeline.py`, `src/code_violation_pipeline.py`

## Entry Points

**CLI entry point:**
- Location: `src/main.py:1005` `cli_main()`
- Triggers: `python src/main.py <mode> [args]`
- Responsibilities: Argparse mode dispatch (15+ modes), preflight checks, logging setup, top-level error → Slack notification, dispatch to `_run_*` helpers

**Apify Actor entry point:**
- Location: `src/main.py:126` `actor_main()` (invoked by `src/__main__.py:asyncio.run(actor_main())`)
- Triggers: `python -m src` (Docker `CMD ["python", "src/main.py"]` calls `actor_main` when `APIFY_IS_AT_HOME` is set)
- Responsibilities: Read Actor input from `Actor.get_input()`, hydrate config from input, run scrape → enrich → Tracerfy → CSVs → KVS → Drive → Slack pipeline, push records to Apify dataset

**Per-domain orchestrator entry points:**
- `src/tax_distress_pipeline.py:_main(argv)` — direct CLI
- `src/code_violation_pipeline.py:_main(argv)` — direct CLI
- `src/extract_market_finder.py:main()` — direct CLI (Playwright)
- `src/probate_property_locator.py` — direct CLI for ad-hoc decedent lookup

**County adapter direct invocation:**
- Each `*_api.py` module has its own `__main__` CLI for ad-hoc testing (e.g. `python src/madison_tax_delinquent_api.py --tax-sale-only`)

**DataSift management modes:**
- `src/main.py:939` `_run_manage_presets` — Playwright preset discovery + sold-exclusion bulk update + sequence creation
- `src/main.py:980` `_run_manage_sold` — Playwright SiftMap sold-property tagging

## Architectural Constraints

- **Threading:** Python `asyncio` for all Playwright-based I/O (scraper, photo download, DataSift uploader); enrichment pipeline is synchronous. `asyncio.run(...)` bridges between sync CLI dispatch and async scrape.
- **Global state:** `src/config.py` is a module-level config singleton — credentials are loaded from `.env` at import time; Apify Actor mutates `config.*` and `os.environ` after reading input. No other module-level mutable state.
- **Working directory:** All `src/` imports assume `src/` is on `PYTHONPATH`. Run from project root with `python src/main.py` (which prepends `src/` to `sys.path` via `__main__.py`) or set `PYTHONPATH=src`. Apify Dockerfile sets `ENV PYTHONPATH=/home/myuser/src`.
- **Import discipline:** Heavy modules (`obituary_enricher`, `entity_researcher`, `report_generator`, `datasift_uploader`) are imported lazily inside functions to keep CLI startup fast and let the pipeline degrade gracefully when optional deps are missing.
- **Single-process state files:** `last_run.json`, `seen_ids.json`, `dropbox_state.json` etc. assume a single concurrent runner. Apify deployment uses Actor KVS keys instead of these files.
- **Playwright sessions:** APN session cookies persist via `cookies.json`; DataSift cookies persist via `datasift_cookies.json`; both are reused across runs to avoid re-login + 2Captcha cost.
- **2Captcha cost on every detail page:** APN gates every notice behind reCAPTCHA v2 (no batching possible); ~10-30s per notice. This is the primary scrape bottleneck.

## Anti-Patterns

### Generic address fallback regex

**What happens:** Older parsers tried `_ADDR_PART` against the raw notice body when high-confidence indicators failed.
**Why it's wrong:** Notice text contains courthouse, auction-location, trustee-office, and mortgage-recording addresses; a generic regex returns one of those instead of the actual property.
**Do this instead:** Only extract addresses after `_PROP_INDICATOR` phrases (`commonly known as`, `property address`, `property street address for informational purposes`). Leave `address=""` rather than guess. See `src/notice_parser.py:234`.

### Tesseract OSD on phone photos

**What happens:** Calling `image_utils.fix_rotation()` (Tesseract OSD) on raw phone images.
**Why it's wrong:** OSD often fails on terminal-screen photos and the 270° fallback rotates correct images sideways.
**Do this instead:** Rely on EXIF transpose only for phone photos; reserve `fix_rotation()` for scanned PDFs where there's no EXIF metadata. See `src/photo_importer.py` vs `src/pdf_importer.py`.

### Standard CLAHE + adaptive threshold for terminal screens

**What happens:** Running `cv2.adaptiveThreshold(...)` after CLAHE — the documented "best practice" for OCR.
**Why it's wrong:** Moire pattern from terminal screens defeats adaptive thresholding; output is unreadable garbage.
**Do this instead:** Bilateral filter (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges, then Otsu threshold (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`). PSM 4, not PSM 6. See `src/photo_importer.py:46`.

### Tag obituary directly to PR for probate

**What happens:** Running obituary search on the PR/executor name from a probate notice.
**Why it's wrong:** The PR is alive. The obituary belongs to the decedent. Searching the PR returns wrong-person matches and can override the court-named executor with an unrelated heir.
**Do this instead:** For probate notices with `decedent_name + owner_name` (PR), set DM = the named PR directly and skip obituary search. See "probate preset" logic in `src/obituary_enricher.py`.

### Run obituary without DOD sanity check

**What happens:** Accepting any obituary that name-matches a notice.
**Why it's wrong:** Common names ("John Smith") return obituaries from decades-old deaths; the matched person is not the property owner.
**Do this instead:** Reject matches where `DOD` is more than 3 years before the notice filing date (`MAX_DOD_GAP_YEARS = 3`). See `src/obituary_enricher.py`.

### CSV column rename

**What happens:** Renaming a column in `data_formatter.SIFT_COLUMNS` or `datasift_formatter._build_row()`.
**Why it's wrong:** Downstream Apify dataset views, DataSift custom-field mappings, and the analysis layer all assume specific column names. Renames break uploads silently.
**Do this instead:** Add new columns at the end of the list; never reorder or rename existing ones. See `src/data_formatter.py:16` and `src/datasift_formatter.py`.

### Hard-coded auction date

**What happens:** Stamping a literal `"2026-05-05"` for tax-sale `auction_date`.
**Why it's wrong:** Date drifts past auction; needs annual code update.
**Do this instead:** Use `next_al_tax_sale_date()` to compute the next first-Tuesday-of-May dynamically. See `src/tax_distress_pipeline.py:53`.

## Error Handling

**Strategy:** Best-effort enrichment with structured logging — every external call is wrapped, failures are logged with context, and the pipeline continues. Top-level handlers post errors to Slack via `src/slack_notifier.py:notify_error`.

**Patterns:**
- `try/except ImportError` around optional enricher imports inside `run_enrichment_pipeline` — missing module is logged as a warning, step is skipped.
- `try/except Exception as e` around every external API call inside enrichers — `logger.warning("  X failed: %s", e)` then continue.
- Atomic state-file writes (`src/config.py:save_state`) — write to `.tmp`, rename, with `.bak` snapshot of previous version.
- Apify Actor wraps the entire pipeline in a `try/except` that calls `Actor.fail(status_message=...)` after posting to Slack.
- CAPTCHA exhaustion: notices that fail all retries are persisted to `captcha_failed_ids.json` so the next run's summary can surface them rather than silently dropping.

## Cross-Cutting Concerns

**Logging:**
- Standard `logging` module via `logger = logging.getLogger(__name__)` in every module
- CLI: `setup_logging()` configures stdout (UTF-8 forced) + date-stamped log file in `logs/scrape_YYYY-MM-DD_HHMMSS.log`
- Apify: `logging.basicConfig(level=INFO, ...)` so all module loggers emit through Actor.log
- Pipeline summaries use `logger.info("══ Pipeline Summary (%s) ══", ...)` and per-step `── Step N: Name ──` headers

**Validation:**
- Schema-level: `_validate_records()` in `src/enrichment_pipeline.py:218` enforces address/city/zip presence (or PR mailing for probate/divorce) and YYYY-MM-DD date format
- Field-level: regex parsers in `src/notice_parser.py` use `_is_valid_name()` to reject "the undersigned" / "Of The Estate" / similar junk captures
- Garbage-OCR detection: `_GARBAGE_RE` rejects pure-non-alphanumeric address strings

**Authentication:**
- All API keys loaded from `.env` via `python-dotenv` at `src/config.py` import time
- Apify mode overrides `config.*` + `os.environ` from `Actor.get_input()` so downstream modules reading either source work
- DataSift + APN + Ancestry: Playwright with persisted-cookie sessions
- Dropbox: OAuth2 refresh token → auto-rotated access tokens
- Google Drive: service-account JSON (base64-encoded for Apify input)

**Deployment:**
- **Local CLI:** `python src/main.py <mode>` — reads `.env`, writes to `output/` and `logs/` directly
- **Apify Actor:** Docker image based on `apify/actor-python-playwright:3.12`, `python -m src` entry point, output to Apify dataset + KVS + Google Drive
- `.actor/actor.json` defines actor manifest, dataset views; `.actor/input_schema.json` defines Apify Console UI for input fields

---

*Architecture analysis: 2026-04-30*
