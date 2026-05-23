# Context Intel

> Synthesized DOC-type context extracted from 3 ingested planning documents.
> Produced by `gsd-doc-synthesizer` on 2026-05-23. Mode: `new`.
>
> Precedence (per-doc manifest override): CLAUDE.md (0) > README.md (1) > FOIA (2).
> Where two docs cover the same topic, CLAUDE.md is the authoritative source
> and README/FOIA additions are appended as supplementary detail without
> overwriting CLAUDE.md's claims.

---

## Document Inventory

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
- Precedence: 0 (highest)
- Title: CLAUDE.md — SiftStack
- Role: Comprehensive system documentation; covers all 5 distressor pipelines
  across 3 Alabama counties, the 80-column DataSift CSV schema, the REI Skill
  Library, courthouse-photo pipeline, Apify deployment, and DataSift UI
  automation patterns.

source: /Users/shanismith/Desktop/SiftStack/README.md
- Precedence: 1 (mid)
- Title: SiftStack
- Role: Public-facing project README; introduces the platform, lists the 5
  intake methods, the 10-step enrichment pipeline, deal-analysis CLI tools,
  buy-box configuration flags, API cost estimates, and the REI Skill Library.
  Frames the project as market-agnostic (Knox/Blount TN reference market) even
  though CLAUDE.md describes the live AL deployment.

source: /Users/shanismith/Desktop/SiftStack/docs/foia/huntsville_code_enforcement_request.md
- Precedence: 2 (low)
- Title: Huntsville Code Enforcement — Public Records Request
- Role: Copy-paste-ready Alabama Open Records Act request letter to close the
  Madison-side soft-violation coverage gap. Includes routing, body template,
  field-to-pipeline cross-reference, tag-fire matrix, and the post-response
  adapter integration plan.

---

## Topic: Platform Overview

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
SiftStack is a full-stack real estate investing operations platform built
around the DataSift.ai CRM. Covers the entire REI lifecycle: data acquisition,
enrichment, deal analysis, market intelligence, CRM automation, lead
management, operations. Plus a REI Skill Library — 13 Claude Co-Work
`.skill`/`.plugin` ZIPs distributed via learn.datasift.ai/claude-skills-rei.

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Project Overview is documented as covering Knox + Blount counties Tennessee
in the top-of-document Project Overview block, but the body of CLAUDE.md
documents the live deployment across three Alabama counties (Jefferson,
Madison, Marshall) with full distressor pipelines. The TN scope appears to
be a historical/foundation framing carried over from earlier builds.

source: /Users/shanismith/Desktop/SiftStack/README.md
README.md frames the project as market-agnostic with Knox/Blount TN as the
built-in reference. Documents that the 7-notice-type parsing pipeline
(foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation,
divorce) works for any county. README does NOT mention the live Alabama
deployment, the Jefferson/Madison/Marshall pipelines, the Tier 1/Tier 2 ZIP
gate, or any of the post-probate / pre-probate orchestrators that CLAUDE.md
documents as operational.

(See "Documentation-scope divergence" entry in INGEST-CONFLICTS.md INFO bucket.)

---

## Topic: Data Intake Methods

source: /Users/shanismith/Desktop/SiftStack/README.md
Five intake methods all converge on the same `NoticeData` records and the
same enrichment pipeline:
1. Web Scrape — Playwright + CAPTCHA solving (CAPTCHA sites, county clerk portals)
2. PDF Import — pypdfium2 rendering + Tesseract OCR (scanned tax-sale lists, legal docs)
3. Photo Import — OpenCV preprocessing + OCR + LLM parsing (courthouse terminal phone photos)
4. Dropbox Watch — auto-polls a Dropbox folder every 15 minutes (runner uploads from field)
5. CSV Re-Import — re-enrich existing data against latest APIs

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
CLAUDE.md adds Apify cloud deployment as the production environment for
scheduled daily runs. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set,
`main.py` switches from CLI args to the Actor SDK.

---

## Topic: Enrichment Pipeline (10 steps)

source: /Users/shanismith/Desktop/SiftStack/README.md
1. Deduplicate (keep most recent)
2. Vacant Land Filter (drop no-house-number parcels)
3. Entity Filter (flag LLC/Corp owners)
4. Probate Property Lookup (3-tier: Tax API → Executor family → People search)
5. Tax Delinquency lookup
6. Address Standardization (Smarty USPS validation, ZIP+4, geocoding, vacancy)
7. Commercial Filter (RDI check)
8. Zillow Enrichment (Zestimate, MLS, equity, details)
9. Obituary Search (deceased detection, heir ID, DM ranking)
10. Data Validation (catch garbage OCR, verify required fields, compute mailable flag)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
CLAUDE.md documents that the AL-specific orchestrators (benchmark_pipeline_al,
apn_probate_pipeline_al, pre_probate_pipeline_al, tax_distress_pipeline,
distress_proxy_pipeline, code_violation_pipeline) each chain their own
distressor-specific waterfall ON TOP of the shared enrichment pipeline.
The shared pipeline runs after the distressor-specific lookup/ZIP gate.

---

## Topic: Alabama Foreclosure Pipeline (Jefferson + Madison)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Both counties run the same end-to-end pipeline. Only difference: detail-page
format. Jefferson returns searchable text, Madison returns image-PDFs or
text-layer PDFs from the publishing newspaper. Three-tier extraction fallback:
1. DOM text via `page.inner_text("body")` — works for Jefferson
2. PDF text via pdfminer — works for most Madison notices (text-layer publishers)
3. PDF OCR via pypdfium2 + Tesseract (PSM 3, 200 DPI) — fallback for image-only scans

`notice_parser._normalize_pdf_text()` is critical: de-hyphenates column-wrapped
words (`in-\nformational` → `informational`), normalizes smart quotes. Without
this every regex spanning more than one PDF column fails on Madison newspapers.

Foreclosure Field Extraction Matrix covers 10 fields populated by the same
parsing path for both counties — date_added (search row), notice_type (snippet
+ INCLUDE_PHRASES), owner_name (executed by ...), owner_first/last_name
(suffix-aware split), address/city/zip (AL indicator "Property street address
for informational purposes:" + bistate regex), auction_date (last "until DATE"
in POSTPONEMENT_RE chain — most-recent reschedule wins), mortgage_company
(Mortgagee/Transferee), original_lender (originally in favor of), trustee
(law firm), trustee_file_number (case dedup).

LLM trigger: foreclosure notices missing any of address/owner_name/auction_date/
mortgage_company/trustee after regex are sent to Claude Haiku for second-pass
extraction.

County filter `is_target_county()` uses three regex patterns to detect the
property's actual county and drops notices for non-target counties. Current
target set: `{"jefferson", "madison"}` — Marshall is added per the Marshall
expansion entry below.

---

## Topic: Alabama Probate Pipeline (Jefferson + Madison) — APN newspaper publications

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Probate Notice-to-Creditors follow the tightly-templated format mandated by
AL Code § 43-2-61 (publish 3 successive weeks) and § 43-2-350 (creditors have
6 months from grant of letters to file). Notice body always contains case
number, Judge of Probate name, and grant date — but NEVER the decedent's
property address. The pipeline fills that gap.

**Notice metadata** extracted via regex + LLM fallback: case_number, decedent
name, PR/Executor/Administrator name, judge_name, granted_date, creditor_deadline
(computed = granted_date + 6 months), PR mailing address.

**Property-address enrichment** uses per-county adapters:
- Madison: AssuranceWeb `madisonproperty.countygovservices.com` via httpx
  (no Playwright). GET search form for CSRF token → POST search with
  `PropertySearchType=name` + criteria → regex+json.loads against the inlined
  Kendo Grid records.
- Jefferson: E-Ring `jeffersonexpress.capturecama.com/SearchRP` REST API via
  httpx (Incapsula SPA front door is bypassable). Two gotchas: `MigratedOwners`
  field uses a double space between surname and given name (adapter
  auto-normalizes); host serves an incomplete SSL chain, so adapter sets
  `verify=False`.

**Decedent-flagged tax-roll markers** (highest-confidence match signal):
- Madison: `(HEIRS OF)`, `(ESTATE OF)`, `LIFE ESTATE ... REMAINDER`, `X HEIRS OF`
- Jefferson: `(D)`, `& X (D)`, `X (D) & Y`, `X AGT FOR HEIRS OF`,
  `X AGT OF HEIRS FOR Y`

**Lookup waterfall** (`probate_property_locator.enrich_notice_with_property`):
1. Tier 1 — decedent-name search in matching county adapter, token-overlap
   scored with +0.2 bonus for deceased-marker matches
2. Tier 2 — PR-name search if Tier 1 misses (catches surviving-spouse PR)
3. Tiers 3 & 4 (people-search, Tracerfy) live in separate downstream modules

**Multi-parcel return**: primary residence + all additional parcels.
Primary-residence selection priority:
`(is_homestead AND deceased_flagged) > is_homestead > total_value > score`.

Homestead heuristics:
- Jefferson: `improvement_value > 0` AND mailing == situs AND (non-empty
  ExmtCode OR improvement_value > $10K)
- Madison: `is_buildable` (situs has non-zero house number — drops "0 STREET" vacants)

**PR-name extraction** tries 3 patterns in priority order:
1. PROBATE_NAME_GRANTED_RE — "having been granted to NAME on/as..."
2. PROBATE_NAME_BEFORE_TITLE_RE — AL signature-block "NAME\nPersonal Representative"
3. PROBATE_NAME_RE — TN-style "Personal Representative: NAME" (backcompat)

**Probate subtypes** assigned by `_parse_probate_subtype()`:
- `probate_sale` — explicit sale-of-real-property notice. Populates
  petition_filed_date, hearing_date, estate_purpose, sale_type, co_pr_names.
- `probate_heirs_notice` — "NOTICE TO: NAME1, NAME2..." (2+ comma-sep all-caps).
  Populates heirs_named_in_notice (pipe-delimited, max 10).
- `probate_creditors` — default, when neither above matches.

probate_sale is the most-valuable subtype for deal flow: PR has explicitly
decided to sell. Filter on `notice_subtype = "probate_sale"` AND `hearing_date`
in the next 30 days for the high-touch sequence.

---

## Topic: Alabama Post-Probate Pipeline (Jefferson — Benchmark Web)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Independent SECOND probate pipeline (in addition to the APN newspaper one).
Pulls cases directly from Jefferson County Probate Court's case-management
system at `benchmarkweb.jccal.org` (login required) — same source the
RealSupermarket vendor uses. Covers the full Jefferson probate case stream
(far more comprehensive than what publishes in newspapers). Madison NOT
supported — no Benchmark equivalent.

Pipeline flow: Benchmark case list → per-case Jefferson property API search
by decedent → ZIP gate (Tier 1 ∪ Tier 2) → fiduciary detection (skip
attorneys appearing in 2+ cases) → obituary cross-reference (DDG →
Ancestry/Newspapers fallback) → PR fallback when obit fails → Tracerfy
batch skip-trace → heir-phone promotion to NoticeData phone slots →
DataSift CSV + Slack notification.

Obituary extraction (single Claude Haiku call, ~1500 tokens) returns the
full family graph: decedent identification + DOD sanity inputs, petitioner
cross-match, all_survivors, spouse, preceded_in_death, executor_named.
`rank_decision_makers()` consumes survivors + executor and produces a
ranked list with signing_authority flags per AL intestate succession law
(executor > spouse > children > siblings).

Ancestry/Newspapers fallback (`--ancestry-fallback`) walks SSDI →
Ancestry obituary collection → Newspapers.com cascade with a 3-year
DOD sanity gate. One-time interactive bootstrap required to seed
`.ancestry_profile/Default/` cookies past CAPTCHA/MFA.

Realistic 14-day batch yields 1-3 fully-enriched leads + 1-3 PR-fallback
leads. Conversion rates: ~15-20% ZIP-gate survival, ~50-60% obituary
confirmation, ~30% skip-trace phone match.

What's NOT done: daily scheduler/cron (manual invocation), Madison
post-probate (no Benchmark), vacancy enrichment.

---

## Topic: Alabama APN Post-Probate Pipeline (Jefferson + Madison + Marshall)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
The canonical Madison post-probate path. Pulls the same shape of data
from alabamapublicnotices.com publications — the only public Madison
post-probate source. Jefferson runs through both Benchmark AND APN
(Benchmark = full case stream; APN = formally published subset).
Marshall added 2026-05-12 — `_TARGET_COUNTIES` and `_adapter_for("Marshall")`
both updated.

Pipeline flow: scrape APN for probate Notice-to-Creditors → per-notice
`enrich_notice_with_property` (Tier 1 decedent + Tier 2 PR fallback) →
Madison-only Smarty one-shot to recover missing ZIP → ZIP gate
(Tier 1 ∪ Tier 2) → Tracerfy → heir-phone promotion → DataSift CSV
(Lists="Probate") + Slack.

Why APN is canonical for Madison: Madison's online probate portal
`madisonprobate.countygovservices.com` is recording-only (Azure AD B2C,
14 categories: DEEDS, MORTGAGES, JUDGMENTS, UCC, etc.) — NO "Estate Cases"
or "Letters Testamentary" category. No case-management view at all.

Cost: ~$0.003/notice (2Captcha) × 5-15 new/day = ~$0.05/day after
`seen_ids.json` warms up. First-run on a fresh cache burns ~$3-5
processing the full backlog.

What's NOT done: Bonds-as-LT-proxy adapter (deferred — ~3-4hr build to
catch non-publishing PRs via the Probate Bonds recording category),
daily scheduler.

---

## Topic: Alabama Pre-Probate Pipeline (Jefferson + Madison + Marshall) — obituary-driven

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Companion to Post-Probate. Where post-probate is case-driven (30-90 days
after death), pre-probate is death-driven (days fresh). Together they
replicate the full RealSupermarket vendor product.

Sources:
- legacy.com Birmingham listing → Jefferson property API
- legacy.com Huntsville listing → Madison property API
- legacy.com Marshall County aggregator → Marshall property API
(Harvester tags each `HarvestedObit` with county_hint and the orchestrator
routes lookups to hinted county first, falls back to the others for
cross-county ownership.)

URL upgrade is the critical fix: legacy.com person pages always show
preview-only snippets. `_fetch_full_obit_text()` auto-follows the
cross-link to obits.al.com for full extractable text. Without upgrade
~80% of obits get rejected as "not an obituary" by the LLM.

Three-path property search waterfall:
1. By decedent name (primary) — both county adapters with last-first
   reorder + middle-name truncation fallbacks
2. By obit-stated address (fallback A) — searches by situs via
   `jefferson_property_api.search_by_situs_address()` when LLM extracted
   `decedent_obit_address`. Verified +16pp recall lift.
3. By spouse name (fallback B) — searches by spouse, validates decedent
   last-name appears in matched owner_name. Catches "surviving spouse
   stays in home, decedent never went on title".

Ground-truth recall vs RealSupermarket April 2026 dump (2,438 Jefferson
records): in our addressable Tier 1+2 pool (1,002 records, 41.1%),
recall is ~67% name-only, ~92% after Path 2 address-fallback.

Madison adapter returns LESS data than Jefferson — only parcel_number,
owner_name, situs street, is_buildable, is_delinquent. `_smarty_zip_for_madison_address`
(now generalized as `_smarty_zip_for_assuranceweb_address`) recovers
city + ZIP per match.

Per-day yield: 1-3 enriched leads (best case). Per-run cost: ~$0.06-0.30
(Firecrawl + Tracerfy).

Setting `notice_type = "pre_probate"` causes DataSift formatter to set
Lists="Pre-Probate/Deceased" with the standard tag block plus signing
chain markers.

What's NOT done: full-text partner-site fetch (~2hr fix), daily scheduler,
ground-truth precision check.

---

## Topic: Alabama Tax-Delinquent + Tax-Sale Pipeline (Jefferson + Madison)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Tax sale / delinquent data is BULK-LIST shaped (not one-notice-per-case),
so APN scraping isn't worthwhile — county portals serve clean structured
data directly.

**Madison adapter** — single GET to `/Property/Property/DelinquentParcels`
returns the entire delinquent list (~600 parcels) inlined as a Kendo Grid
JSON array. No auth, no pagination, no AJAX. Each `MadisonDelinquentRecord`
includes a pre-flagged `is_tax_sale_parcel` boolean (Madison already
identifies which delinquent parcels go to the May auction).

Filters: `tax_sale_only`, `individuals_only` (drops `BUSINESS_RE` matches
but keeps trusts and HEIRS/ESTATE), `min_balance` (recommended $5,000).
Phase 1 yield: 11 high-quality leads from 634 raw via individuals + $5K floor.

**Jefferson adapter** — Tax Collector publishes the annual auction roster
on jccal.org. Fetches iframed HTML tables under `/Sites/Jefferson_County/
Documents/{year}/{Birmingham|Bessemer}TaxTable-{year}.html`. ~18,225 raw
parcels per year (~12,794 Birmingham + ~5,431 Bessemer). BeautifulSoup
parses table, single HTTP call per district, ~3 sec total wall time.

Important: this list IS the tax-sale roster (not just delinquencies), so
the converter sets `notice_type="tax_sale"` for every record per AL § 40-10-180.

Situs-address parsing handles single-string `PropertyAddress` like
`"426 18TH ST BHAM AL 35218"` via state+ZIP tail regex + Jefferson-cities
allowlist (longest-first). When no city matches, leaves city empty rather
than guessing; downstream Smarty fills from ZIP.

**Unified pipeline** `tax_distress_pipeline.py` runs both adapters
sequentially (~5sec wall), applies auction-date stamping, optionally
writes both CSV formats. Phase 1 combined yield: 321 high-exposure
individual-owner records ($2.97M total balance, $205M assessed value).

**Phase 3 auction-date stamping** — both counties hold auctions in early
May per AL § 40-10-15. `next_al_tax_sale_date()` computes the next
first-Tuesday-of-May (today: 2026-05-05). `apply_auction_dates()` stamps
that date on every tax_sale-typed notice without one. Madison records
typed `tax_delinquent` (not on the pre-flagged auction subset) are left
without auction_date — those aren't on this year's roster.

DataSift integration uses existing 80-column slots. Five tags fire
automatically: `tax_delinquent` (balance > 0), `tax_high_exposure`
(≥ $5K), `tax_high_exposure_10k` (≥ $10K), `individual_owner` (no
BUSINESS_RE match), `entity_owned` (inverse).

**Distress-Proxy Pipeline** (`distress_proxy_pipeline.py`) — synthetic
distress-proxy stopgap for the Huntsville code-enforcement coverage gap.
Layers: tax-delinquent ≥ $5K + individuals only + Madison Smarty ZIP
recovery + ZIP gate + Jefferson ABSENTEE detection (mailing != situs;
~68% of in-tier Jefferson records). Tags `tier_distress_proxy` or
`tier_distress_proxy_absentee` (Jefferson absentee subset).
2026-05-12 sample: Jefferson returned 84 in-tier (27% of 310 high-exposure
individuals), 57 absentee. Madison returned 0 (post-auction feed reset).

---

## Topic: Alabama Code-Violation Pipeline (Jefferson + Madison)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Code-enforcement data is shaped completely differently from tax/probate/
foreclosure — no symmetric two-county adapter pattern because cities expose
violations in completely different ways.

**Phase 1 — Huntsville Unsafe Buildings (Madison)** — monthly PDF at
`/wp-content/uploads/{YYYY}/{MM}/{MM}-{YYYY}-Unsafe-Building-List.pdf`.
Adapter auto-discovers most recent published list by walking back 6 months.
pdfminer parses the 3-column layout (Date / Case# / Address). Uses
`requests` instead of httpx because huntsvilleal.gov's WAF fingerprints
httpx and 403s it. ~220 active cases per snapshot. Every record fires both
`unsafe_building` and `demolish` tags — every parcel has been formally
declared uninhabitable, so the outreach framing is tear-down economics.

**Phase 2 — APN code-violation scraper** — 6 SAVED_SEARCHES (3 per county)
with TIGHTENED keywords because naive single-keyword searches had 100%
false-positive rates (CONDEMNATION alone catches drug/firearm forfeitures
+ ALDOT eminent-domain; DEMOLITION alone catches construction bid
solicitations). AND-combined keywords + `bid contractor sealed` excludes:
- DEMOLITION + UNSAFE STRUCTURE
- CONDEMNED STRUCTURE DEMOLITION
- NUISANCE ABATEMENT DEMOLISHED

Coverage reality: all real teardown publications come from Tuscaloosa/
Mobile/Albertville. Birmingham/Huntsville post 1-5 Jefferson/Madison
teardown publications per quarter. Filter is clean but volume is sparse.

**Phase 3 — Madison owner enrichment** — `madison_property_api.search_by_situs_address()`
fills owner_name + parcel_id by re-piping the Huntsville unsafe-building
situs back through the Madison property API in address-search mode. Adapter
handles three normalization layers: suffix+directional stripping, unit/
parenthetical stripping, spelled-ordinal→digit form ("Tenth Ave Sw"→"10th").
~80% hit rate on the April 2026 unsafe-building list. ~20% misses are
multi-unit condos with non-standard parcel formats, or properties not on
the current tax roll.

**Phase 4 — Birmingham Accela early-distress scraper** — Playwright-based
adapter for `aca-prod.accela.com/BIRMINGHAM` (Accela is ASP.NET WebForms
with `__VIEWSTATE` postbacks). Pulls 5 enforcement record-types:
Condemnation, Housing, Inoperable Vehicles, Environmental, Zoning.
Adds the new `early_distress` tag (owner still in property but slipping
on maintenance — months-to-years before foreclosure/unsafe-building).
Outreach framing is rehab/cleanup offer, NOT demolish framing. Per 30-day
window: ~300-500 new Birmingham distress cases.

Optional `--enrich-details` clicks each case for owner_name + mailing
+ fees + Accela deceased flags (~3sec/record extra). Optional
`--enrich-owner` uses Jefferson E-Ring `search_by_situs_address`
(searchtype=4) for owner+parcel without Accela session (~0.3sec/record,
~80% hit rate). Directional-fallback retry handles `1124 SW 16TH ST SW`
formats.

**Phase 6 — Hoover SeeClickFix code-enforcement** (added 2026-05-08) —
citizen-reported complaints via SeeClickFix's API. Anti-bot bypass requires
browser-like User-Agent + Referer. Filter by lat/lng+zoom (33.4054,
-86.8114, zoom=11) + post-filter strict "hoover" in address.
`notice_subtype="code_enforcement_complaint"` (softer signal than
unsafe_building). ~30 in-tier issues per 30 days across 35226 (T1, 19),
35216 (T2, 6), 35244 (T2, 5).

**Birmingham-metro dead-ends researched 2026-05-08** — Vestavia Hills
(Connect + OpenGov permits-only), Trussville (Freshdesk auth-gated),
Hueytown (govtportal payments-only), Pinson (114-byte website, no infra).
SeeClickFix probe returned 0-7 issues/city. Decision: stay APN-only for
these 4 cities. Path to close is FOIA, parallel to the Huntsville request.

**Coverage gap** (planned to close via FOIA) — Huntsville's softer
violations (tall grass, inoperable vehicles, IPMC, zoning) are NOT
covered by the public scrape. Closing requires recurring AL Open Records
Act request to Huntsville's Community Development / Code Enforcement.
Letter template lives at `docs/foia/huntsville_code_enforcement_request.md`.

**Unified orchestrator** `code_violation_pipeline.py` runs all city
adapters in one pass. Per-city wrappers: `_fetch_madison()` (Huntsville),
`_fetch_jefferson()` (Birmingham Accela), `_fetch_hoover()` (SeeClickFix —
fires when Jefferson selected AND include_hoover=True). Passes through
all per-city knobs.

---

## Topic: Marshall County Distressor Coverage (added 2026-05-12)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Marshall is the third active county after Jefferson + Madison. The build
economy was large because Marshall shares Madison's vendor platform
(AssuranceWeb / countygovservices.com) for property search and tax-delinquent
recording. The Madison adapter patterns clone almost directly.

**Tier ZIPs** (codified in `src/target_zips.py` as `MARSHALL_TIER_1` /
`MARSHALL_TIER_2`):
- Tier 1: 35950, 35976, 35016, 35961, 35951, 35957 (Albertville core +
  Guntersville + Arab + Boaz + Crossville/Geraldine + Albertville fringe)
- Tier 2: 35962, 35175, 35747, 35769, 35980 (Boaz outer + 4 border ZIPs
  for cross-county ownership: Cullman, Grant, Jackson/Scottsboro, DeKalb)

Codification is a union of MD volume rankings AND operator-strategic
additions; MD source-of-truth analysis at `~/Documents/Claude/Projects/
REI Skill Library/Marshall_County_AL_SFR_125K_500K_Market_Analysis.md`.

**Coverage matrix** (all distressors live as of 2026-05-12, EXCEPT
tax-delinquent which is county-disabled):
- Property search: AssuranceWeb `marshall.countygovservices.com` — LIVE
  (clone of madison_property_api; 936 SMITH hits, 14 SMITH JOHN hits verified)
- Probate (post-probate): APN + `_search_marshall` enrichment — LIVE
- Pre-probate: legacy.com Marshall County aggregator — LIVE
- Foreclosure: 2 SAVED_SEARCHES — LIVE
- Code-violation: APN-floor only (no online municipal source) — LIVE
- Tax-delinquent: AssuranceWeb DelinquentParcels — STUB.
  Source disabled by county ("The Delinquent Parcels listing is currently
  disabled" + "Payments are currently disabled" as of 2026-05-12). Adapter
  probes the page on each call via `is_source_disabled()` and returns []
  while offline. Orchestrators (`tax_distress_pipeline.py`,
  `distress_proxy_pipeline.py`) wire the stub through. When Marshall
  re-enables, back-fill the parser — `NotImplementedError` raises today
  if the page comes back online (by design — prevent silent data loss).

Marshall has no Accela/SeeClickFix/municipal-portal exposure for code
violations. Albertville/Boaz/Guntersville/Arab all handle code enforcement
through internal staff workflows with no public read. SeeClickFix probe
returned essentially zero issues. Same posture as Birmingham-metro
dead-end cities.

`_smarty_zip_for_marshall_address()` uses `"Albertville AL"` as
USPS-CASS anchor — generalized as `_smarty_zip_for_assuranceweb_address`
for any AL counties on the same vendor platform in future.

Pre-probate orchestrator's `search_order` rotates all 3 counties with
the hinted county first — catches "decedent died in Huntsville but
owned property in Boaz" cross-county cases.

---

## Topic: DataSift.ai (REISift) Integration

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
DataSift.ai (formerly REISift) is the CRM where scraped records land for
niche sequential marketing. **There is NO REST API** — upload is via
Playwright browser automation of the web UI.

Domain: `app.reisift.io` (NOT `app.datasift.ai`). API host: `apiv2.reisift.io`.

**CSV column structure (80 columns as of 2026-04)** — 10 core auto-mapped
(Property + Owner addresses), 17 phone/email/meta (Phone 1-9 from Tracerfy,
Email 1-5, Tags, Lists, Notes), 18 built-in fields (valuations, tax, parcel,
structure, beds/baths/sqft), 15 SiftStack custom fields (Notice Type, County,
Date Added, Owner Deceased, Decedent + DM details, Source URL), 10 deep
prospecting (DM Status, Heir Count, Signing Chain), 3 entity research,
7 AL probate enrichment (Case Number, Judge, Subtype, Petition/Hearing
Dates, Creditor Deadline, Total Estate Value).

**Built-in field mapping nuances (probate)**: Probate Open Date prefers
granted_date over date_added. Personal Representative uses owner_name
(verified PR) before decision_maker_name (may be obituary-derived heir).
Estimated Value falls back to assessed_value when Zillow hasn't run.
Structure Type falls back to property_use (assessor classification) when
Zillow hasn't populated property_type.

**Niche sequential marketing** — 2 preset folders: "00 Niche Sequential
Marketing" (12 presets, courthouse data) + "01. Bulk Sequential Marketing"
(9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23).
"Sold Property Cleanup" sequence in Transactions folder auto-fires on
"Sold" tag — change status, remove from lists, clear tasks, clear assignee.

**Tags strategy** — every record gets "Courthouse Data" + notice_type +
county + YYYY-MM + deceased/living + DM confidence level + has_auction +
tax_delinquent + photo_import. AL probate adds municipality_* (from
lowercased Jefferson DispCode), homestead, probate_sale/probate_creditors/
probate_heirs_notice, multi_parcel, hearing_upcoming (≤30 days),
creditor_window_open.

**Lists routing** — `NOTICE_TYPE_TO_LIST` maps notice_type to DataSift
list name. The per-row `Lists` column does NOT auto-route — upload wizard
creates ONE target list per session. The canonical pipeline
(`upload_to_datasift_per_distressor`) SPLITS the CSV by Notice Type and
uploads each subset using `existing_list=True` with the mapped list name.
Each target list must already exist in DataSift. Tax-sale records are
intentionally consolidated under "Tax Delinquent" — auction-roster
distinction preserved on the notice_type=tax_sale tag.

**Heir CSVs** — heir rows land in the same distressor list as the source
DM row, distinguished by per-row `heir_of_<notice_type>` tag. Filter
presets target with e.g. `foreclosure AND heir_of_foreclosure` to
isolate heir audience.

**Upload Wizard (5 steps)**: Setup (Upload File → Add Data → "Adding to
existing list" mode is canonical; legacy "Uploading new list not in
DataSift yet" mode used by deprecated `upload_to_datasift` helper) → Tags
(skip) → Upload File (set on `<input type="file">`) → Map Columns (core
address auto-maps; Tags/Lists/Estimated Value need manual drag-drop) →
Review + Finish Upload (background processing).

Update Data mode has a different downstream sequence (modal interrupts
file-input step) and times out the existing helper — canonical helper
uses Add Data mode + existing-list selection. Records matched on address
by DataSift; duplicate creation possible without manual cleanup.

**Post-upload automation**: Enrich Property Information (Manage → Enrich
Data, ON by default; "Enrich Owners" + "Swap Owners" are OFF to protect
PR/DM mapping). Skip Trace (Send To → Skip Trace, ON by default,
unlimited plan @ $97/mo, auto-tags `skip_traced_YYYY-MM`).

**Login selectors** — hidden checkboxes via `<label>` clicks not
`<input>`. Use `wait_until="domcontentloaded"` not `networkidle`
(SPA keeps WebSocket open). Cookie validation: check for `/dashboard`
or `/records` in URL with 5s wait.

**UI automation patterns** — DataSift is styled-components (no native
HTML controls). All dropdowns are `[class*="Selectstyles__Select"]`
containers. Filter panel scrolling requires JS (`scrollIntoView`) not
Playwright's `scroll_into_view_if_needed`. Sequence Builder card drag
needs slow mouse-drag with 20 incremental steps (cards have
`draggable="false"`). Pointer-interception blockers: Beamer NPS survey
iframe (`#npsIframeContainer`) and push modal (`#beamerPushModal`) must
be DOM-removed before clicks. RecordsFiltersstyles__RecordsFiltersSection
intercepts — use `page.evaluate(el => el.click())`.

**Market Finder Extraction Patterns (build 1.0.29+)** — table is div-based
not HTML `<table>`. Pagination (20 rows/page) not infinite scroll —
Knox=48 ZIPs (3 pages), 120+ neighborhoods (7 pages). State/County
selection via `InputMultiSearch` placeholders. ZIP/Neighborhood toggle
is a styled Select — check BEFORE clicking (toggle away if already on
correct view). Page body scrolling required to expose pagination
controls below the viewport.

---

## Topic: Courthouse Photo Pipeline (build 1.0.28+)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Runner takes phone photos at Knox/Blount county terminals, uploads to
Dropbox organized as `{county}/{notice_type}/`, system auto-processes.
Supports all 7 notice types: existing 4 from web scraper plus eviction
(plaintiff=landlord=target), code_violation (owner of record + violation
+ deadline), divorce (petitioner+respondent, property from schedule page).

**Critical OCR patterns** — moire pattern from terminal screens is the
#1 OCR killer. Standard preprocessing (adaptive threshold, CLAHE)
produces garbage. The fix: bilateral filter
(`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving
text edges. Otsu threshold (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after
bilateral. PSM 4 (single column variable text) for terminal screens NOT
PSM 6 (single uniform block) — research recommended 6, practice required 4.
Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos — EXIF
transpose handles rotation; OSD on raw phone images often fails and the
270° fallback rotates correct images sideways.

**Probate deep prospecting** — courthouse probate records have decedent
+ PR name but NO property address. Multi-tier lookup:
1. Knox Tax API name search (token-overlap scoring, FIRST MIDDLE LAST
   vs LAST FIRST MIDDLE, accept ≥ 0.4 match; tries multiple variations
   with/without suffix)
2. Executor family search (look for decedent's last name in
   executor's owned properties)
3. People search (TruePeopleSearch/FastPeopleSearch for decedent's
   last known Knox address)

**Probate preset** in obituary enricher — triggers when court record has
PR + decedent (no address required), prevents wrong obituary from
overriding court-named executor. Sets DM = the named PR/executor
directly, skips obituary search, then runs DM address lookup (Knox Tax
API → People Search → Tracerfy).

**DOD sanity check** — `MAX_DOD_GAP_YEARS = 3`. Rejects obituary
matches where DOD is > 3 years before notice filing. Applied to both
full-page and snippet matches.

Dropbox env vars: DROPBOX_APP_KEY, DROPBOX_APP_SECRET,
DROPBOX_REFRESH_TOKEN (auto-rotates access tokens), DROPBOX_POLL_INTERVAL
(default 900 = 15 min), DROPBOX_ROOT_FOLDER.

---

## Topic: Apify Cloud Deployment

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
Runs as an Apify Actor. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set,
`main.py` uses the Actor SDK instead of CLI args. Daily schedule
configurable in Apify Console.

Actor Input: mode (daily/historical), counties/types arrays, secrets
(tn_username/password/captcha_api_key), optional Google Drive folder ID +
service account key.

Actor Output: Dataset (via `Actor.push_data()`), Key-value store
(`output.csv` backup), optional Google Drive (CSV + summary text file
via service account).

Key files: `.actor/actor.json` (Actor manifest), `.actor/input_schema.json`
(input fields + validation for Console UI), `Dockerfile` (based on
`apify/actor-python-playwright:3.12`), `src/drive_uploader.py` (Google
Drive upload via base64-encoded service account key), `input.json`
(local test, gitignored).

source: /Users/shanismith/Desktop/SiftStack/README.md
README documents Apify as the production environment for scheduled daily
automation. Install: `npm install -g apify-cli`. Deploy: `apify login` +
`apify push`. Schedule + secrets in Console. Runs the full pipeline:
scrape → enrich → skip trace → DataSift upload → Slack notify.

---

## Topic: Saved Searches + County Configuration

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
`SAVED_SEARCHES` in `config.py` defines keyword searches against
alabamapublicnotices.com. Each `SearchConfig` is
`(county, notice_type, search_terms, search_type, exclude_terms, days_back)`.
New optional field: `notice_subtype` — when set, scraper writes it
through to `notice.notice_subtype` for every record from that search.
Used exclusively for code-violation searches today; available for
future search-driven subtype classification.

Active counties (May 2026): Jefferson + Madison + Marshall (added
2026-05-12). All three have same pillar coverage: foreclosure +
probate + pre-probate + code-violation. Marshall property + probate
adapters share Madison's AssuranceWeb platform. Marshall tax-delinquent
is county-disabled stub.

Probate searches use `AND` on `Estate Deceased` with
`foreclosure mortgage` excluded — chosen empirically because APN's
search box has no county filter (statewide full-text). The
county-of-property check happens later in `is_target_county()` against
the full notice text.

Filterable via `--counties` and `--types` CLI args (comma-separated;
omit for all).

---

## Topic: Key Domain Rules

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
- Foreclosure filtering is critical. Not all notices from "Foreclosure"
  saved searches are actual foreclosures. Scraper parses each notice's
  full text and only includes ones with trustee sale language. See
  `INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`.
- Probate `owner_name` should be the Personal Representative / Executor /
  Administrator — NOT the deceased.
- Owner names in foreclosure notices typically appear after "executed by"
  in the deed of trust language.
- Rate limiting: 2-3 second random delays between requests, 3 retries
  per page.
- Address dedup: same property can appear in multiple notices;
  `data_formatter.deduplicate()` keeps the most recent.

---

## Topic: Notice Types (7 Total)

source: /Users/shanismith/Desktop/SiftStack/README.md
| Type | Source | Look For |
|---|---|---|
| Foreclosure | Web scrape, PDF | Trustee sale, deed of trust default |
| Tax Sale | Web scrape, PDF | Delinquent property tax auction |
| Tax Delinquent | Web scrape, PDF | Tax lien, unpaid property taxes |
| Probate | Web scrape, Photos | Estate admin, executor appointment |
| Eviction | Photos | Landlord-tenant, detainer warrant |
| Code Violation | Photos | Building code, compliance deadline |
| Divorce | Photos | Property division, marital assets |

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
CLAUDE.md aligns on the same 7 types and adds the source-channel detail:
foreclosure/tax_sale/tax_delinquent/probate come through web scraper
AND APN AND photos; eviction/code_violation/divorce are
photo-import-driven. Code violation also has the dedicated Phase 1-6
pipeline (Huntsville PDF, Birmingham Accela, Hoover SeeClickFix, APN
formal teardowns).

---

## Topic: Buy Box Configuration

source: /Users/shanismith/Desktop/SiftStack/README.md
By default SiftStack filters out property types that don't fit a typical
residential wholesaling buy box. Three toggle flags:
- `--include-vacant` (default OFF) — keep vacant land parcels
- `--include-commercial` (default OFF) — keep commercial properties
- `--include-entities` (default OFF) — keep LLC/Corp/Trust-owned records

Examples:
- Default — residential only
- Land investor — `--include-vacant`
- Commercial — `--include-vacant --include-commercial --include-entities`
- Entity researcher — `--include-entities --research-entities`

Same toggles available as checkboxes in Apify Console under the Actor's
input configuration. Apply to every scheduled run.

---

## Topic: API Configuration & Cost Estimates

source: /Users/shanismith/Desktop/SiftStack/README.md
Required for web scraping:
- TNPN_EMAIL / TNPN_PASSWORD — state's public notice site (free account)
- CAPTCHA_API_KEY — 2Captcha (~$3/1,000 solves)

Enrichment APIs (optional, pipeline degrades gracefully):
- SMARTY_AUTH_ID / TOKEN — Smarty (250 free/month) — USPS validation, geocoding, vacancy
- OPENWEBNINJA_API_KEY — OpenWeb Ninja (100 free/month) — Zestimate, MLS, equity
- ANTHROPIC_API_KEY — Anthropic (~$0.001/record) — LLM parsing, obituary search
- TRACERFY_API_KEY — Tracerfy ($0.02/record) — phones, emails, mailing
- TRESTLE_API_KEY — Trestle ($0.015/phone) — phone scoring (5-tier dial priority)

DataSift + notifications:
- DATASIFT_EMAIL / PASSWORD — DataSift.ai (auto-upload + SiftMap enrich + skip trace)
- SLACK_WEBHOOK_URL — Slack / Discord (daily summaries + error alerts)

Optional intake:
- DROPBOX_APP_KEY / SECRET / TOKEN — auto-poll for courthouse photos
- ANCESTRY_EMAIL / PASSWORD — SSDI + obituary collection

Monthly cost estimate (Knox TN, ~20-40 new notices/day): 2Captcha $3 +
Smarty Free + OpenWeb Ninja Free + Anthropic Haiku $2 + Tracerfy $20 +
Trestle $15 = ~$40/month total. Requires DataSift.ai subscription
($97/month unlimited skip trace plan).

---

## Topic: REI Skill Library (13 Skills)

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
13 distribution-ready Claude Co-Work skill files at
`Skills for REI/improved/`. Each `.skill` is a ZIP of SKILL.md +
references/. Plugins (`.plugin`) also include commands/ +
`.claude-plugin/plugin.json`. Distributed via
learn.datasift.ai/claude-skills-rei.

Inventory (13 total):
1. sift-market-research.skill — Market Intel — score 9.6
2. first-market-county-data.skill — Market Intel — 9.7
3. buyer-prospector.skill — Market Intel — 9.6
4. real-estate-comping.skill — Deal Analysis — 9.7
5. rehab-estimator.skill — Deal Analysis — 9.8
6. deal-analyzer.plugin — Deal Analysis — 9.6
7. deep-prospecting.skill — Deal Analysis — 9.6
8. probate-property-finder.skill — Deal Analysis — 9.7
9. phone-validator.skill — Operations — 9.8
10. sequential-presets.skill — Operations — 9.5
11. sift-sequences.skill — CRM — 9.5
12. sift-operations.plugin — CRM — 9.3
13. playbook-creator.skill — Operations — 9.5

**Cross-skill verified consistency** (identical across all referencing skills):
- Phone tiers: 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial
  Third), 21-40 (Dial Fourth), 0-20 (Drop)
- Preset folders: "00 Niche Sequential Marketing" (12), "01. Bulk
  Sequential Marketing" (9)
- Sequence count: 26 TCA templates across 5 folders (Lead Management 6,
  Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- Comp adjustments: Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr
  (from comp_analyzer.py)
- Financing defaults: HML 12%, conventional 7%, 2 points, 2.5% closing
  (from deal_analyzer.py)
- DOD sanity: MAX_DOD_GAP_YEARS = 3 (from obituary_enricher.py)
- Notice types: 7 total (foreclosure, tax_sale, tax_delinquent, probate,
  eviction, code_violation, divorce)

**Key corrections during April 2026 optimization**:
- Hardcoded credentials removed from sift-market-research
- Bedroom adjustment corrected from $10K to $5K in real-estate-comping
  (matched to comp_analyzer.py)
- HML points corrected from 0% to 2% in deal-analyzer (matched to
  deal_analyzer.py DEFAULT_HARD_MONEY_POINTS)
- Linux paths fixed in sequential-presets (was /home/ubuntu/skills/...,
  now relative)
- Preset names aligned across 3 skills to match niche_sequential.py
- Transfer tax labeled Tennessee-specific in deal-analyzer with state
  reference table
- "Substantial renovation" defined in real-estate-comping: kitchen + 1
  bath minimum (~$15K spend)

source: /Users/shanismith/Desktop/SiftStack/README.md
README documents the same 13-skill inventory at a higher level (skill
name + one-line description) and points to
learn.datasift.ai/claude-skills-rei for distribution.

---

## Topic: Huntsville Code Enforcement FOIA Request

source: /Users/shanismith/Desktop/SiftStack/docs/foia/huntsville_code_enforcement_request.md
Copy-paste-ready Alabama Open Records Act request letter (AL Code
§ 36-12-40) to close the Madison-side coverage gap on early-distress
code violations. Birmingham (Jefferson) exposes housing, inoperable-
vehicle, environmental, and zoning enforcement via the public Accela
portal; Huntsville only exposes the formal Unsafe Buildings list.

**Routing**:
- Primary: City Clerk's Office, 308 Fountain Circle SW, Huntsville AL 35801
  (clerk@huntsvilleal.gov)
- Cc: Community Development — Code Enforcement Division, 320 Fountain
  Circle / P.O. Box 308, Huntsville AL 35804 (codeenforcement@huntsvilleal.gov)

**7 categories requested**:
- a. Property Maintenance Code (IPMC) violations
- b. Overgrowth / nuisance vegetation
- c. Inoperable / abandoned vehicles
- d. Junk / debris / refuse / bulk-trash
- e. Zoning enforcement
- f. Environmental / public-nuisance
- g. Demolition/condemnation/unsafe-building cases NOT already in the
  monthly Unsafe Buildings PDF

**14 fields requested per record**: case number, dates (opened, last
activity, compliance deadline), status, category, description, address
(situs + parcel), owner (name + mailing), fees (total + balance),
inspector ID. Explicitly NOT requesting investigative work-product,
attorney-client comms, photos, complainant identities, or other
exempt fields.

**Delivery preference**: CSV/Excel direct export > structured PDF >
SQL/JSON. Recurring MONTHLY delivery on first business day of each
month, covering prior calendar month, until withdrawn in writing.

**Fee posture**: willing to pay reasonable duplication + staff time
costs. Fee cap recommended at $50 initial backfill / $25 per recurring
monthly. Fee waiver requested on public-interest grounds (reducing
blight, restoring tax base, returning vacant homes to occupancy; data
not for resale).

**Response timeline**: 7 business days to acknowledge, 20 business days
for substantive response or fee estimate.

**Field-to-pipeline cross-reference** — every requested field is
deliberately mapped to the existing `NoticeData` schema and
`BirminghamEnforcementRecord` dataclass so the Huntsville response
slots into the existing pipeline without per-record manual cleanup:
- Case number → `case_number` (DataSift Notes column) — dedup across monthly pulls
- Date opened → `date_added` / `notice_date` — marketing window
- Last activity → recency tag — long-festering = higher conversion
- Status → filter (drop already-resolved)
- Violation category → `notice_subtype` (tags housing_enforcement,
  inoperable_vehicle, environmental_enforcement, zoning_enforcement —
  mirroring Birmingham)
- Description → DataSift Notes (cold-call agent reference)
- Address → `address` / `city` / `zip` (primary key for Madison
  `search_by_situs_address()` owner enrichment)
- Parcel → `parcel_id` (skips address-search step when present)
- Owner → `owner_name` / `tax_owner_name` (DataSift Property Owner First/Last)
- Mailing → `mailing_*` (DataSift mailing block; absentee = stronger lead)
- Compliance deadline → filter (about to escalate = motivated-seller signal)
- Total fees → `tax_delinquent_amount` (same slot Birmingham uses; no
  separate code-fee column in schema; fires `tax_high_exposure` ≥ $5K)
- Outstanding balance → overrides total when present (real motivation signal)
- Inspector → extra dict (optional, spot-check data quality)

**Tag matrix** (mirrors Birmingham strategy so a single DataSift filter
preset works across both counties):
- housing_enforcement → `housing_enforcement, early_distress, code_violation, madison, courthouse_data`
- inoperable_vehicle → `inoperable_vehicle, early_distress, code_violation, madison, courthouse_data`
- environmental_enforcement → `environmental_enforcement, early_distress, code_violation, madison, courthouse_data`
- zoning_enforcement → `zoning_enforcement, early_distress, code_violation, madison, courthouse_data`
- unsafe_building (existing Phase 1) → `unsafe_building, demolish, code_violation, madison, courthouse_data`
- tax_delinquent_amount ≥ $5K → adds `tax_high_exposure`

`early_distress` is the bottom-of-funnel signal: owner still in property
but slipping on maintenance. Reach-out window months-to-years before
foreclosure. Outreach framing is rehab/cleanup offer, NOT the tear-down
framing the `demolish` tag uses.

**Post-response pipeline integration plan**:
1. Source loader — read monthly CSV/Excel/PDF from Dropbox folder
   (e.g. `TN Public Notice/Madison/code_violation_soft/`) using same
   pattern as `dropbox_watcher.py`
2. Record dataclass — `HuntsvilleCodeViolationRecord` mirroring
   `BirminghamEnforcementRecord`
3. `fetch_code_violations()` — read latest export, filter by
   `min_age_months` (mirror `min_age_years` from Phase 1)
4. `enrich_with_owner()` — already exists on Madison adapter
   (`madison_property_api.search_by_situs_address()`), ~80% hit-rate
   pattern from Phase 3
5. `to_notice_data()` — sets `notice_type="code_violation"` + matching
   subtype, fires `early_distress` (or `demolish` for unsafe-building
   escalations)
6. Wire into `code_violation_pipeline.py._fetch_madison()` to merge soft
   violations alongside the existing Unsafe Buildings PDF

Once flowing, soft-violation tags land in DataSift on the same monthly
cadence as the Birmingham early-distress feed.

---

## Topic: Output, Logs, Architecture Map

source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
CSV files land in `output/` (gitignored). Logs in `logs/` with timestamped
filenames. Sift columns: date_added, address, city, state, zip,
owner_name, notice_type, county, source_url.

source: /Users/shanismith/Desktop/SiftStack/README.md
README documents the `src/` module map — 37 source files including:
main.py (CLI + Apify entry), scraper.py + captcha_solver.py +
notice_parser.py + foreclosure_filter.py (web acquisition); pdf_importer
+ photo_importer + dropbox_watcher + image_utils + llm_parser + llm_client
(intake); enrichment_pipeline + address_standardizer + property_enricher +
tax_enricher + property_lookup + obituary_enricher + ancestry_enricher +
entity_researcher + tracerfy_skip_tracer + phone_validator (enrichment);
data_formatter + datasift_formatter + datasift_uploader (DataSift);
comp_analyzer + deal_analyzer + rehab_estimator + market_analyzer +
buyer_prospector + deep_prospector + lead_manager + sequence_templates +
niche_sequential + playbook_generator + report_generator + excel_exporter
+ drive_uploader + slack_notifier + config (analysis + ops).

source: /Users/shanismith/Desktop/SiftStack/README.md
README documents MIT license + standard fork/branch/PR contribution flow.
Built by DataSift.ai for the REI community.
