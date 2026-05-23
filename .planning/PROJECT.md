# SiftStack

## What This Is

SiftStack is a full-stack real-estate-investing operations platform built around the DataSift.ai CRM. It covers the entire REI business lifecycle for a solo wholesaler/investor: data acquisition from county courthouses and public-notice sites, multi-source enrichment, deal analysis, market intelligence, CRM automation, lead management, and operations tooling. Today it is operational across **Jefferson, Madison, and Marshall counties, Alabama** with five active distressor pipelines (foreclosure, probate, pre-probate, tax-distress, code-violation) and an APN newspaper backbone, plus a 13-skill REI Skill Library distributed to the broader DataSift community.

## Core Value

**Convert public-record distress signals into actionable, skip-traced, in-tier, decision-maker-attached leads inside DataSift on a low-cost, daily, hands-off cadence — at sub-cent-per-record economics that beat third-party vendors like RealSupermarket.**

If everything else fails, this must work: a notice or obituary in a Tier 1/Tier 2 AL ZIP → enriched `NoticeData` row → DataSift list with the right Lists/Tags/phones → Slack action card.

## Requirements

### Validated

<!-- Shipped and confirmed valuable (v1.0 — operational as of 2026-05-23). -->

- ✓ APN web-scrape ingestion for Jefferson + Madison + Marshall (foreclosure, probate, pre-probate, code-violation keyword sets) — v1.0
- ✓ Three-tier foreclosure extraction (DOM → pdfminer → pypdfium2+Tesseract) with `_normalize_pdf_text()` de-hyphenation — v1.0
- ✓ AL probate metadata extraction (`case_number`, `judge_name`, `granted_date`, `creditor_deadline`, probate subtypes: `probate_sale` / `probate_heirs_notice` / `probate_creditors`) — v1.0
- ✓ Probate property locator with Tier 1 (decedent) + Tier 2 (PR) waterfall against Madison AssuranceWeb + Jefferson E-Ring + Marshall AssuranceWeb adapters — v1.0
- ✓ Multi-parcel decedent return with primary-residence ranking (homestead × deceased-flag × value × score) — v1.0
- ✓ Jefferson Benchmark Web post-probate pipeline (login-required case-management system, 14-day batch, ZIP gate, fiduciary detection, obituary cross-reference, PR fallback) — v1.0
- ✓ APN post-probate pipeline (canonical Madison path; Jefferson dual-source with Benchmark; Marshall added 2026-05-12) — v1.0
- ✓ Pre-probate pipeline (legacy.com Birmingham + Huntsville + Marshall County aggregators → county-routed property API → ZIP gate → Tracerfy) with URL upgrade (`legacy.com/person/*` → `obits.al.com/*`) — v1.0
- ✓ Pre-probate three-path search waterfall: decedent name → obit-stated address → spouse name (verified +16pp recall lift to ~92%) — v1.0
- ✓ Tax-delinquent + tax-sale pipeline for Madison (bulk Kendo Grid feed) + Jefferson (Birmingham + Bessemer HTML tax tables) with auction-date stamping (`next_al_tax_sale_date()`) — v1.0
- ✓ Distress-proxy pipeline (tax-delinquent ≥ $5K + individuals + Smarty ZIP recovery + ZIP gate + Jefferson ABSENTEE detection) as Huntsville code-enforcement stopgap — v1.0
- ✓ Code-violation pipeline: Huntsville Unsafe Buildings PDF (Phase 1), APN tightened-keyword scrape (Phase 2), Madison + Jefferson owner enrichment via address-search (Phases 3 + 5), Birmingham Accela early-distress scraper (Phase 4), Hoover SeeClickFix (Phase 6) — v1.0
- ✓ Marshall County coverage matrix: AssuranceWeb property + probate + pre-probate + foreclosure + APN-floor code-violation live; tax-delinquent stub auto-activates on county re-enable — v1.0
- ✓ Tier 1/Tier 2 ZIP gate codified in `src/target_zips.py` for all 3 counties (33 priority ZIPs total: 18 T1 + 15 T2) — v1.0
- ✓ Smarty ZIP recovery for AssuranceWeb-platform counties (`_smarty_zip_for_assuranceweb_address`) — v1.0
- ✓ Unified 10-step enrichment pipeline with idempotent skip flags and graceful degradation (Smarty → Zillow → Tax → Obituary → Validate → Mailable) — v1.0
- ✓ Obituary enricher with single-call family-graph LLM extraction + DOD sanity check (`MAX_DOD_GAP_YEARS = 3`) + Ancestry/Newspapers fallback — v1.0
- ✓ Tracerfy batch skip-trace with heir-phone promotion to NoticeData Phone N / Email N slots — v1.0
- ✓ Trestle phone validator (5-tier dial priority: 81-100 → 0-20) — v1.0
- ✓ DataSift CSV formatter (80-column schema with 15 SiftStack custom + 7 AL probate enrichment fields) — v1.0
- ✓ DataSift Playwright uploader: login, 5-step Add Data wizard (existing-list mode), per-distressor split upload, enrich, skip-trace, preset management, sequence builder, SiftMap sold tagging — v1.0
- ✓ Apify Actor deployment (`apify/actor-python-playwright:3.12` base; daily-schedulable; Dataset + KVS + optional Drive output) — v1.0
- ✓ Courthouse photo pipeline (Dropbox auto-poll → OpenCV bilateral + Otsu → Tesseract PSM 4 → LLM parse) supporting all 7 notice types (Knox/Blount TN legacy path) — v1.0
- ✓ Slack/Discord webhook daily notifications with per-pipeline action cards — v1.0
- ✓ REI Skill Library: 13 distribution-ready `.skill` / `.plugin` ZIPs published to learn.datasift.ai/claude-skills-rei with cross-skill value consistency — v1.0
- ✓ Analysis layer: comp analyzer (Two-Bucket ARV), rehab estimator (4-tier), deal analyzer (MAO + financing), market analyzer (6-factor zip scoring), buyer prospector, deep prospector (4-level) — v1.0

### Active

<!-- Current scope for the v1.1 active milestone — captured from "What's NOT done" notes throughout CLAUDE.md. -->

- [ ] **OPS-01**: Unified daily scheduler across all 5 pipelines + Marshall coverage (replaces per-pipeline manual invocation; cron-style or Apify-Schedule-driven)
- [ ] **OPS-02**: Single daily Slack post that unifies all DataSift leads across pipelines (currently per-pipeline; user wants one consolidated daily funnel post — see MEMORY note `unified_slack_target`)
- [ ] **OPS-03**: Always emit the full pipeline funnel (drop counts at every gate) on every run so the user can audit conversion vs assume a break (see MEMORY note `run_funnel_transparency`)
- [ ] **PROB-MAD-01**: Madison post-probate Bonds-as-LT-proxy adapter via `madisonprobate.countygovservices.com` Probate Bonds category (~3-4hr build; catches non-publishing PRs that APN misses)
- [ ] **PREPROB-01**: Full-text partner-site fetch for pre-probate (funeral-home pages: Welch, Larkin & Scott, dignitymemorial, etc.) — recovers family graph for the ~30-40% of obits that drop after legacy → obits.al.com upgrade still produces preview-only text (~2hr fix)
- [ ] **PREPROB-02**: Ground-truth precision check of pre-probate output against `REISift_Upload_Jefferson_Pre-Probate.csv` (the 2,438-row RealSupermarket April 2026 dump) — recall is measured (~92%) but precision has not been
- [ ] **ENR-01**: Vacancy enrichment for inherited probate properties (USPS NCOA + water-shutoff signals) to tighten lead score with "actually unoccupied" confirmation
- [ ] **CODE-MAD-01**: Huntsville FOIA-driven soft-violation adapter (`huntsville_code_violations_api.py`) parallel to Birmingham Accela, wired into `code_violation_pipeline._fetch_madison()` once monthly export from City Clerk is flowing — see `docs/foia/huntsville_code_enforcement_request.md`
- [ ] **TAX-MAR-01**: Marshall tax-delinquent parser back-fill — replace the `NotImplementedError` stub in `marshall_tax_delinquent_api.fetch_delinquent_parcels()` the moment Marshall County re-enables the AssuranceWeb DelinquentParcels listing
- [ ] **BUGFIX-01**: Madison `_search_madison` name-format bug — currently passes `(parts[0], parts[1:])` which queries "FIRST" as last name (per CONCERNS.md); add Jefferson-style last-first retry. Madison probate property-locator hit rate is silently degraded
- [ ] **BUGFIX-02**: Apify Actor cold-start `AttributeError` on dead `config.TNPN_EMAIL` / `config.TNPN_PASSWORD` at `src/main.py:184` — every scheduled Apify run currently crashes before scraping
- [ ] **BUGFIX-03**: `_GARBAGE_RE` mismatch with its docstring (matches non-alphanumeric, not non-letter) — numeric-only OCR'd addresses pass validation and ship to DataSift as garbage
- [ ] **PARSER-01**: Add `PR_ADDRESS_NAME_FIRST_RE` (`<name>\n<title>\n<address>`) to `notice_parser._parse_pr_address()` — AL probate PR mailing addresses systematically missing when ANTHROPIC_API_KEY is unset / Haiku rate-limited
- [ ] **OBS-01**: Per-service success-rate metric on Slack daily report (2Captcha solve rate, Smarty hit rate, Tracerfy match rate, LLM extraction success rate) — current pattern is "fewer records 24-48h later"

### Out of Scope

<!-- Explicit boundaries with reasoning to prevent re-adding. -->

- **Birmingham-metro municipal code-enforcement scrapers for Vestavia / Trussville / Hueytown / Pinson** — researched 2026-05-08; all 4 confirmed not publicly scrapeable (Connect/OpenGov permits-only, Freshdesk auth-gated, govtportal payments-only, no infrastructure). Stay APN-only for these cities. FOIA is the only path to close, parallel to Huntsville — defer until APN volume proves insufficient
- **Marshall code-enforcement online scraper** — researched 2026-05-12; no Accela / SeeClickFix / municipal portal exposure for Albertville / Boaz / Guntersville / Arab. Internal staff workflows with no public read. APN-floor scraper carries coverage
- **Real-time DataSift REST API** — DataSift exposes NO REST API. Upload is via Playwright UI automation. Do not chase a hypothetical API
- **DataSift "Update Data" wizard mode** — different downstream sequence than "Add Data"; modal interrupts file-input step; existing helper times out. Canonical helper stays on Add Data + existing-list. Re-investigate only if DataSift redesigns the wizard
- **Multi-instance concurrent CLI runs** — local state files (`last_run.json`, `seen_ids.json`, `cookies.json`, etc.) are single-machine. Production runs through Apify KVS; local CLI is single-developer
- **TN-county (Knox/Blount) production scraping** — README framing is historical/aspirational; live deployment is AL-only. TN code paths preserved but not maintained for daily-ops use
- **Renaming any existing `data_formatter.SIFT_COLUMNS` or `datasift_formatter._build_row()` columns** — Apify dataset views, DataSift custom-field mappings, and analysis layer all depend on names. Add new columns at the end only
- **Hardcoding annual auction dates** — use `next_al_tax_sale_date()` dynamic computation, never literal year-stamping
- **Tax-roll address fallback regex on raw notice body** — pulls courthouse / auction-location / trustee-office addresses. Only extract after `_PROP_INDICATOR` phrases; leave `address=""` rather than guess

## Context

### Operator profile
Solo wholesaler/investor (the user) running an REI business out of `~/Desktop/SiftStack`. GitHub identity `propsolvers-sketch`. SSH key at `~/.ssh/id_ed25519`. Canonical remote is `propsolvers-sketch/SiftStack` (moved off `tyvhb` on 2026-05-01; `tyvhb` is read-only upstream). Primary IDE: VS Code with Claude Code extension. Email: propsolvers@gmail.com. Daily Slack will eventually be unified across all lead types.

### Operational maturity
This is a **mature, operational platform**, not a greenfield. Five distressor pipelines are live across 3 counties. The codebase is ~54 modules in `src/` (Python 3.12). Production runtime is Apify Actor cloud; local CLI is single-developer dev path. All planning artifacts in `.planning/` (this initialization) reflect that maturity: Validated requirements describe what has shipped; Active requirements are the open backlog items.

### Tech stack
Python 3.12 / Playwright (async) / Apify SDK / httpx / requests / Anthropic Claude Haiku / Smarty USPS / OpenWebNinja (Zillow) / Tracerfy / Trestle / 2Captcha / Tesseract + OpenCV / pypdfium2 + pdfminer / Dropbox SDK / Google Drive (service account) / Firecrawl / Serper / ddgs / DataSift.ai (UI automation only — no REST). Single-file Dockerfile (`apify/actor-python-playwright:3.12` base).

### Data sources (live)
- **APN newspaper publications** (alabamapublicnotices.com) — foreclosure / probate / pre-probate / code-violation across all 3 counties, gated by reCAPTCHA v2 (~$0.003/notice via 2Captcha)
- **Jefferson Benchmark Web** (`benchmarkweb.jccal.org`) — login-required probate case-management system
- **Jefferson E-Ring Capture API** (`jeffersonexpress.capturecama.com/SearchRP`) — property search by owner + situs (bypasses Incapsula SPA front door)
- **Madison AssuranceWeb** (`madisonproperty.countygovservices.com`) — property search + delinquent parcels (Kendo Grid JSON inlined)
- **Marshall AssuranceWeb** (`marshall.countygovservices.com`) — property search (live); delinquent parcels (county-disabled stub)
- **Jefferson Tax Collector** (`jccal.org/.../{Birmingham|Bessemer}TaxTable-{year}.html`) — annual tax-lien auction roster
- **Huntsville Unsafe Buildings PDF** (`huntsvilleal.gov/wp-content/uploads/...`) — monthly published 3-column PDF
- **Birmingham Accela Citizen Access** (`aca-prod.accela.com/BIRMINGHAM`) — 5 enforcement record types (ASP.NET WebForms + ViewState)
- **Hoover SeeClickFix** (`seeclickfix.com/api/v2/issues`) — citizen-reported complaints (lat/lng + zoom bbox)
- **legacy.com** Birmingham + Huntsville + Marshall County obit aggregators — auto-upgrade to obits.al.com partner pages
- **Ancestry.com SSDI + Newspapers.com** — opt-in obituary gap-closer with 3-year DOD sanity gate

### Tier ZIPs (codified in `src/target_zips.py`)
- Jefferson T1: 35215, 35214, 35022, 35023, 35226, 35235 | T2: 35216, 35126, 35210, 35173, 35244
- Madison T1: 35810, 35811, 35803, 35758, 35805, 35801 | T2: 35757, 35759, 35763, 35806, 35750
- Marshall T1: 35950, 35976, 35016, 35961, 35951, 35957 | T2: 35962, 35175, 35747, 35769, 35980

**Source-of-truth analysis docs live OUTSIDE the repo** at `~/Documents/Claude/Projects/REI Skill Library/*_County_AL_SFR_*_Market_Analysis.md` — Tier defs must stay in sync between MD docs AND `src/target_zips.py`.

### Known operational economics
- APN scraping: ~$0.003/notice (2Captcha) → ~$0.05/day after `seen_ids.json` warms up (first run burns $3-5)
- Pre-probate: ~$0.06-0.30 per run (Firecrawl listing fetches + Tracerfy)
- Tracerfy: ~$0.02 per contact (~$0.06-0.30 per typical batch)
- Realistic per-day enriched yields: 1-3 fully-enriched leads (DM + phone) per pipeline; ~15-20% ZIP-gate survival, ~50-60% obit confirmation, ~30% skip-trace phone match
- Comparison to vendor (RealSupermarket Jefferson pre-probate April 2026 dump, 2,438 records): SiftStack addressable pool is 1,002 records (41.1% in-tier); recall ~92% after Path 2 address fallback; ~38% of vendor volume at sub-cent vs vendor pricing

### Known concerns (carry forward)
See `.planning/codebase/CONCERNS.md` for the full audit. High-priority items lifted into Active requirements above (BUGFIX-01/02/03, PARSER-01). Lower-priority items (cross-county adapter duplication / no shared base, 3 name-splitter implementations, 4,246-line `datasift_uploader.py`, single-file `main.py` mode dispatch, missing Tesseract in Dockerfile, floating dependency versions, SSL `verify=False`) are tech-debt eligible for v2 milestone work.

## Constraints

- **Tech stack**: Python 3.12 pinned via `Dockerfile` base image (`apify/actor-python-playwright:3.12`). Local dev observed on 3.14 system Python; project venv is `.venv/`. No language migration in scope.
- **Deployment runtime**: Apify Actor cloud is the daily production target. Same code must run as local CLI (`python src/main.py <mode>`) when `APIFY_IS_AT_HOME` is unset.
- **Working directory**: All `src/` imports assume `src/` on `PYTHONPATH`. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.
- **CAPTCHA cost**: 2Captcha is the primary bottleneck — required on every single APN detail page (~10-30s per notice). No batching available. `seen_ids.json` dedup is the only cost-control lever.
- **External-service concentration**: 9+ paid external services (2Captcha, Anthropic, Smarty, OpenWebNinja, Tracerfy, Trestle, Serper, Firecrawl, DataSift). Most degrade gracefully; 2Captcha and DataSift are hard dependencies. See CONCERNS.md "External-Service Reliance" table.
- **DataSift**: No REST API — Playwright UI automation only. Selectors are styled-components (`[class*="Selectstyles__Select"]`); React state requires native setter + dispatch; popup blockers (`#beamerPushModal`, `#npsIframeContainer`) must be DOM-removed before clicks. UI changes break upload silently.
- **Canonical schema**: `NoticeData` dataclass (~170 fields, in `src/notice_parser.py`) is the single canonical schema. Every ingestion path emits it; every downstream module depends on it. Renaming or reordering existing fields breaks downstream Apify Dataset views and DataSift custom-field mappings — only ever append.
- **State files single-machine**: `last_run.json`, `seen_ids.json`, `cookies.json`, `dropbox_state.json`, `photo_state.json`, `captcha_failed_ids.json`, `datasift_cookies.json` assume a single concurrent runner. Apify path uses KVS.
- **Solo developer + Claude**: One person (user) + one implementer (Claude). No team coordination, no sprints, no enterprise PM theater. Phases are work buckets, not project-management artifacts.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Single canonical `NoticeData` schema across 7+ ingestion paths and 7 notice types | Decouples ingestion from enrichment; every downstream consumer only depends on this dataclass | ✓ Good — enables idempotent step skipping and re-import flows |
| Always extract `auction_date` from the LAST `until DATE` in the `POSTPONEMENT_RE` chain | Original publication date is stale once postponed; most-recent reschedule wins | ✓ Good — verified across both Jefferson + Madison foreclosure pipelines |
| DataSift upload uses Playwright UI automation, not API | DataSift exposes no REST API; only programmatic path is the web wizard | ✓ Good (forced) — but creates 4,246-line `datasift_uploader.py` monolith |
| Probate pipelines split into Post (case-driven) + Pre (death-driven) to replicate full RealSupermarket vendor coverage | Vendor was the benchmark; matching their coverage shape lets SiftStack replace the buy | ✓ Good — ~92% recall in Jefferson, sub-cent per record |
| Adopt APN newspaper pipeline as canonical Madison post-probate path | Madison probate portal is recording-only (no Estate Cases category); APN is the only public source | ✓ Good — same NoticeData shape as Benchmark output |
| Clone Madison adapter patterns for Marshall (shared AssuranceWeb vendor platform) | Build economy was large because the vendor platform is identical | ✓ Good — Marshall live 2026-05-12, except county-disabled tax-delinquent |
| Stay APN-only for Birmingham-metro dead-end cities (Vestavia / Trussville / Hueytown / Pinson) and Marshall code-enforcement | Researched 2026-05-08 + 2026-05-12; no scrapeable online source. FOIA is the only path | ✓ Good — documented as out-of-scope; APN-floor scraper provides coverage |
| Use Apify KVS for state in production; local JSON files for single-developer CLI | Multi-instance concurrent local CLI runs would silently desync; production needs durable state | ⚠️ Revisit — code-path divergence between `actor_main()` and `cli_main()` is causing drift bugs (see BUGFIX-02) |
| Distress-proxy pipeline as Huntsville code-enforcement stopgap while FOIA pending | FOIA cycle is weeks/months; tax-delinquent + absentee + tier ZIP is ~75% predictive of code-violation status | ✓ Good — defensible Madison-equivalent signal using only data we have |
| Tier 1/Tier 2 ZIP definitions live in BOTH `src/target_zips.py` AND `~/Documents/Claude/Projects/REI Skill Library/*_County_AL_SFR_*_Market_Analysis.md` | Code drives filter; MD docs explain "why each ZIP" | ⚠️ Revisit — keeping both in sync is manual; MEMORY note `reference_siftstack_tier_zips` flags this as recurring risk |
| Add new DataSift CSV columns at the END of the schema; never reorder or rename | Apify dataset views, DataSift custom-field mappings, and analysis-layer CSV readers all assume position/name stability | ✓ Good — captured as Out of Scope rule |

---
*Last updated: 2026-05-23 after doc-ingest bootstrap (`/gsd-new-project` from ingested CLAUDE.md + README.md + FOIA docs)*
