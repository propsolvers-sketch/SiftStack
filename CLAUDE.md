# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** Web scraping tnpublicnotice.com (foreclosures, tax sales, probates), scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, Knox County Tax API, obituary/heir research, Ancestry.com SSDI, Tracerfy skip trace, Trestle phone scoring, entity research
3. **Deal Analysis:** Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing scenarios)
4. **Market Intelligence:** Zip code scoring, Market Finder reports, cash buyer list building, investor portfolio analysis
5. **CRM Automation:** DataSift upload, 26 TCA sequence templates, 12 niche sequential marketing presets, filter preset management, SiftMap sold property tagging
6. **Lead Management:** 4 Pillars of Motivation auto-qualification, STABM daily routine, pipeline reporting, deep prospecting (4-level framework)
7. **Operations:** Acquisition playbook generator (SOPs, scripts, checklists), Slack/Discord notifications, Google Drive upload, Apify Actor deployment

Currently focused on Knox and Blount counties, Tennessee.

8. **REI Skill Library:** 13 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Run
python src/main.py daily                          # new notices since last run
python src/main.py historical                     # last 12 months of data
python src/main.py daily --split                  # separate CSV per county+type
python src/main.py daily --counties Knox          # only Knox county
python src/main.py daily --types foreclosure,probate  # only specific types
python src/main.py daily -v                       # verbose/debug logging

# DataSift preset/sequence management
python src/main.py manage-presets --discover                      # list all presets and sequences
python src/main.py manage-presets --add-sold-exclusion            # add Sold exclusion to all presets
python src/main.py manage-presets --create-sold-sequence          # create Sold cleanup sequence
python src/main.py manage-presets --all                           # discovery + update + sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12                   # tag sold properties (last 12 months)
python src/main.py manage-sold --counties Knox --min-sale-price 5000

# Courthouse photo import (build 1.0.28+)
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type probate
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type eviction --skip-obituary
python src/main.py dropbox-watch                                  # auto-poll Dropbox for new photos
python src/main.py dropbox-watch --poll-interval 300 --max-polls 5  # 5-min interval, 5 cycles
python src/main.py dropbox-watch --no-delete                      # keep photos in Dropbox after processing
```

All source files are in `src/` and imports assume `src/` is the working directory. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.

## Architecture

**Data flows:**
- **Web scrape:** `main.py` → `scraper.py` → `captcha_solver.py` → `notice_parser.py` + `foreclosure_filter.py` → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → `image_utils.py` OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV (auto-polling loop)
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → paginate all ZIP + neighborhood data → JSON → `generate_knox_report.py` → 7-sheet Excel

- **main.py** — CLI entry point. Parses args (`daily`/`historical`, `--split`, `--counties`, `--types`, `-v`). Filters saved searches by county/type, orchestrates scrape → dedup → export, logs run summary stats.
- **scraper.py** — Playwright browser automation. Reuses saved session cookies when possible, falls back to fresh login. Selects each saved search from the Smart Search dropdown (triggers ASP.NET postback), paginates results (50/page max), clicks each View button to open notice detail pages. Uses `last_run.json` for daily mode state, `cookies.json` for session persistence.
- **captcha_solver.py** — Solves reCAPTCHA v2 via **2Captcha API** on every notice detail page. Sends websiteURL + sitekey, gets back a `g-recaptcha-response` token, injects it, clicks "View Notice". Retries up to 3 times. This is the primary bottleneck (~10-30s per notice).
- **notice_parser.py** — Extracts structured fields from raw notice text using regex. There are NO structured HTML fields on the site — address, owner, dates are all embedded in free-text notice bodies. Defines the `NoticeData` dataclass used throughout.
- **foreclosure_filter.py** — Filters foreclosure search results to only keep real first-to-market trustee sales. Matches against observed title variations (substitute/successor trustee sales). Non-foreclosure notice types pass through unfiltered.
- **data_formatter.py** — Deduplicates by address (keeps most recent), then converts `NoticeData` list to Sift upload CSV. Split mode produces `{county}_{type}_{timestamp}.csv` files.
- **config.py** — Credentials (from `.env`), ASP.NET element selectors, saved search definitions, rate limiting constants, paths, image processing thresholds.
- **image_utils.py** — Shared OCR utilities used by both `pdf_importer.py` and `photo_importer.py`. Exports `fix_rotation()` (Tesseract OSD) and `ocr_page(image, psm)` with configurable page segmentation mode. Handles Tesseract binary detection.
- **photo_importer.py** — Courthouse phone photo import. OpenCV preprocessing chain (EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold) → Tesseract OCR (PSM 4) → LLM parsing → NoticeData. Supports all 7 notice types.
- **dropbox_watcher.py** — Cursor-based Dropbox folder polling. Downloads new photos, resolves county + notice_type from folder path (`/Knox/eviction/photo.jpg`), processes through photo_importer, deletes from Dropbox after success. State persisted to `dropbox_state.json` + `photo_state.json`.
- **report_generator.py** — Generates per-record PDF deep prospecting reports using reportlab. Includes property summary, signing chain with phone tiers, valuation, deceased owner detection. Output to `output/reports/`.
- **extract_market_finder.py** — Playwright automation to extract ALL ZIP code + neighborhood data from DataSift Market Finder. Handles styled-component dropdowns, pagination (20 rows/page), Beamer popup dismissal. Outputs JSON. See "Market Finder Extraction Patterns" below.
- **market_analyzer.py** — ZIP code scoring engine. 6-factor weighted composite (Distress 30%, Value 20%, Equity 15%, Tax Delinquency 15%, Competition 10%, DOM 10%). Grades A/B/C/D, budget allocation across top ZIPs. Reads from scraped notice CSVs in `output/`.
- **drive_uploader.py** — Google Drive upload via service account. `upload_file()` (generic, returns webViewLink) and `upload_csv()` (CSV-specific, returns file ID).

## Site-Specific Details

The site is **ASP.NET WebForms** — all navigation uses `__doPostBack()` with ViewState. Session IDs are embedded in URL paths (`/(S({guid}))/`). Playwright is required because direct HTTP requests would need to manage ViewState/EventValidation manually.

**reCAPTCHA v2 is required on every single notice detail page**, even when logged in. There is no CAPTCHA on login, search, or results pages. The sitekey is hardcoded in `config.py`.

## Saved Searches

`SAVED_SEARCHES` in `config.py` defines keyword searches against alabamapublicnotices.com. Each `SearchConfig` is `(county, notice_type, search_terms, search_type, exclude_terms, days_back)`.

Active counties (May 2026):
- **Jefferson County, AL** — foreclosure (MORTGAGE FORECLOSURE SALE / RESCHEDULE) + probate (Estate Deceased) + pre-probate (Notice of Death / Death Notice) + code-violation (DEMOLITION / CONDEMNATION / NUISANCE)
- **Madison County, AL** — same pillar coverage as Jefferson
- **Marshall County, AL** — same pillar coverage as Jefferson + Madison (added 2026-05-12). Property + probate adapters share Madison's AssuranceWeb platform. Tax-delinquent feed is **currently disabled** by the county; the orchestrator-wired stub auto-activates when re-enabled.

Probate searches use `AND` on `Estate Deceased` with `foreclosure mortgage` excluded — chosen empirically because APN's search box has no county filter (statewide full-text), and broader OR-style queries (`Probate Sale Property NOTICE TO CREDITORS Estate`) match every foreclosure SALE. The county-of-property check happens later in `is_target_county()` against the full notice text.

Filterable via `--counties` and `--types` CLI args (comma-separated, or omit for all).

## Alabama Foreclosure Pipeline (Jefferson + Madison)

Both counties use the **same end-to-end pipeline**. The only difference is detail-page format: Jefferson notices come back as searchable text, Madison notices come back as newspaper image-PDFs (or text-layer PDFs depending on the publishing newspaper). The pipeline auto-detects and falls back through three extraction tiers:

1. **DOM text** (`page.inner_text("body")`) — works for Jefferson; minimal/empty for Madison
2. **PDF text via pdfminer** — works for most Madison notices (Madison County Record et al. publish text-layer PDFs)
3. **PDF OCR via pypdfium2 + Tesseract (PSM 3, 200 DPI)** — fallback for image-only newspaper scans (e.g. Speakin' Out News, scanned older issues)

`notice_parser._normalize_pdf_text()` is critical: it de-hyphenates column-wrapped words (`in-\nformational` → `informational`, `Hunts-\nville` → `Huntsville`, `post-\nponed` → `postponed`) and normalizes smart quotes. Without this, every regex that spans more than one PDF column fails on Madison newspapers.

### Foreclosure Field Extraction Matrix

All ten fields below populate from the same parsing path for both counties — no per-county branches. Regex catches the common cases; LLM (`llm_parser.py`) fills the rest when `ANTHROPIC_API_KEY` is set.

| Field | Source | Notes |
|---|---|---|
| `date_added` (published) | Search results row metadata | No CAPTCHA needed; deterministic from the row's `pub_date_raw` |
| `notice_type` (foreclosure / postponement / etc.) | Snippet first line + `foreclosure_filter.INCLUDE_PHRASES` | Snippet visible pre-CAPTCHA |
| `owner_name` | `_parse_name()` regex on `executed by ...` | Works for both states |
| `owner_first_name` / `owner_last_name` | `_split_owner_name()` postprocessing | Strips suffixes (Jr/Sr/III), takes first listed party in joint owners |
| `address` / `city` / `zip` | `_parse_address()` with AL-specific indicator `"Property street address for informational purposes:"` and bistate regex (TN \| AL) | Trailing directional captured (e.g. "Dr SW") |
| `auction_date` | `_parse_auction_date()` — finds `POSTPONEMENT_RE` matches first, returns the LAST `until DATE` in the chain | Critical: original-publication date is stale once postponed; we want the most recent rescheduled date |
| `mortgage_company` (current servicer/transferee) | `_MORTGAGEE_RE` on `the undersigned X, as Mortgagee/Transferee` | Entity name preserved (LLC, P.A., N.A.) via `_clean_entity_name()` |
| `original_lender` | `_ORIGINAL_LENDER_RE` on `originally in favor of X` | Often "MERS as nominee for ..." |
| `trustee` (law firm conducting sale) | `_TRUSTEE_RE` matches `("Transferee") <Law Firm>, P.A.` | Smart quotes normalized first |
| `trustee_file_number` | `_TRUSTEE_FILE_RE` on `File Number: ...` | Useful for de-duping postponements of the same case |

LLM trigger (`needs_llm` in `parse_notice_page`): foreclosure notices that lack any of `address`, `owner_name`, `auction_date`, `mortgage_company`, or `trustee` after regex are sent to Claude Haiku for second-pass extraction.

### County Filter (`is_target_county`)

Three regex patterns detect the property's actual county to filter false positives (search keyword matched a trustee or unrelated reference):
1. `Office of the Judge of Probate of {County} County` — Alabama recording office reference
2. `{County} County Courthouse` / `{County} County, Alabama` — courthouse location
3. `Publication County: {County}` — alabamapublicnotices.com header field

`_TARGET_COUNTIES = {"jefferson", "madison"}`. Notices whose property is in any other county are dropped.

## Alabama Foreclosure — Multi-Source Trustee Portal Coverage (added 2026-07-08 → 07-09)

Alabama's non-judicial foreclosure statute (§ 35-10-13) requires 3 weeks of newspaper publication. APN captures **only** publishers who participate in the alabamapublicnotices.com network — **The Madison County Record is NOT an APN participant** (verified via APN publication-dropdown check 2026-07-08), so Madison + Marshall foreclosures returned 0 from APN alone. Four independent trustee-portal adapters close this gap; combined they give **5 concurrent foreclosure sources** running in the daily GHA cron.

| Source | Adapter | Portal | Volume (weekly, target counties) | Notes |
|---|---|---|---|---|
| APN scrape | `main.py daily` | alabamapublicnotices.com | 20-30 | Turnstile CAPTCHA → 2Captcha. Strong Jefferson, weak Madison/Marshall |
| Rubin Lublin | `src/rubin_lublin_al_pipeline.py` (350d668) | rlselaw.com | 10-15 | Static HTML table, no auth |
| Tiffany & Bosco pending | `src/tiffany_bosco_al_pipeline.py` (c46bd72) | fs.tblaw.com/Sales/PendingSalesAl.aspx | 5-10 | ASP.NET ListView, iagree cookie bypass |
| Halliday Watkins Mann | `src/halliday_watkins_al_pipeline.py` (b4e6a42) | halliday-watkins.com/live/AL/bids.htm | 5-10 | Static HTML; **no ZIP in address column** → tier filter runs POST-enrichment |
| T&B Sales Results | `src/tiffany_bosco_al_results_pipeline.py` (c427ad3) | fs.tblaw.com/Sales/SalesResultsAl.aspx | 3-5 | 180-day POST-back; **Cancelled + Postponed only** (drops Sold/Reverted/3rd-Party) |

**Deferred** (2026-07 evaluation):
- **McMichael Taylor Gray** — Playwright + PowerBI iframe; fragile, low incremental volume
- **LOGS Legal** — Playwright + PowerBI iframe; same fragility profile

### Uniform enrichment chain (as of b63fa6e, 2026-07-09)

All 5 sources run the same 4-stage chain. Consistency is enforced at commit time — new adapters MUST include all 4 stages before the DataSift CSV write:

1. **County property API** — owner name recovery: Jefferson E-Ring (`src/jefferson_property_api.py`), Madison + Marshall AssuranceWeb (`src/madison_property_api.py`, `src/marshall_property_api.py`). Also recovers ZIP for HWM (address column has no ZIP).
2. **Smarty US Street standardization** — populates `dpv_match_code`, `latitude`, `longitude`, `plus4_code` (ZIP+4). License MUST be `us-core-cloud` (not the SDK default `us-standard-cloud`, which lacks USPS-CASS). See `src/address_standardizer._build_client()` (3265316).
3. **Tracerfy skip-trace** — phones + emails. `--skip-trace` flag on each adapter; workflow YAML passes it unconditionally. Cost ~$0.02/contact, ~$0.30-0.60/day across the 3 trustee adapters.
4. **Trestle phone tier scoring** — populates `Phone Tags N` columns for DataSift filter presets (Tier 1 mobile / Tier 2 landline / Tier 3 VOIP).

**City-tier centroid ZIP fallback** (97654c2 + d05f79f, extended to trustee adapters in b63fa6e): when Smarty USPS-CASS doesn't recognize the specific house number but confirms the street is in a known Madison/Marshall city, uses the Tier-1 centroid ZIP from `address_standardizer._CITY_TIER_ZIP_FALLBACK` (Huntsville→35801, Albertville→35950, Arab→35016, Guntersville→35976, Boaz→35957) and stamps `zip_estimated_from_city` in `NoticeData.missing_data_flags` for downstream visibility. HWM adapter also uses this map directly as a hard fallback when both property API and Smarty miss.

### Cross-source deduplication

Three layers:
1. **Within-fetch:** each adapter dedups its own paginated results (T&B Results uses `(file#, sale_date, outcome)` triple; others use address).
2. **Cross-run persistence:** `.tb_results_seen_ids.json` for T&B Results (180-day dedup window). Other adapters re-fetch fully each day and rely on layer 3.
3. **Cross-source at upload time:** DataSift's address-dedup handles overlap between adapters (a property appearing in both RL and T&B pending lands once with the union of enrichment fields).

The state files committed by the bot's `chore(bot)` step: `seen_ids.json`, `last_run.json`, `seen_code_violations.json`, `.tb_results_seen_ids.json`, `output/observability/service_rates.json`.

### CSV output naming (glob pattern for daily_finalize)

Each adapter writes to `output/leads/` with a distinct prefix so `daily_finalize._categorize()` can route them:

- `datasift_upload_foreclosure_<ts>.csv` — APN (main.py daily)
- `datasift_upload_foreclosure_rl_<ts>.csv` — Rubin Lublin
- `datasift_upload_foreclosure_tb_<ts>.csv` — T&B pending
- `datasift_upload_foreclosure_hwm_<ts>.csv` — Halliday Watkins Mann
- `datasift_upload_foreclosure_tb_results_cancelled_<ts>.csv` — T&B Cancelled (subtype `foreclosure_cancelled`)
- `datasift_upload_foreclosure_tb_results_postponed_<ts>.csv` — T&B Postponed (subtype `foreclosure_postponed`)

The `notice_subtype` field on the Results CSVs enables DataSift filter-preset targeting (Cancelled = highest-signal distress).

### Daily Slack summary structure (`scripts/daily_finalize.py`)

Rendered in this order (a1ae8e0 + d63d531 + 3999bec):

1. **Upcoming Auctions** — pinned at top; three time windows:
   - 🚨 next 7 days (highest priority — call immediately)
   - ⚠️ 8-14 days
   - 🕐 15-21 days
   - Each entry: address, city, county, ISO date, days-remaining. Cap 8 rows per window; overflow shown as "...and N more". Cross-source dedup by property address.
2. **Foreclosure Sources Breakdown** — per-source + per-county counts. Reconciles the discrepancy between the APN-only `main.py --notify-slack` post (posted ~90 min earlier as "APN Scrape — early progress") and the consolidated total.
3. **APN funnel** (main.py sub-summary), **Pre-Probate funnel**, **Code Violation funnel** (a1ae8e0) — each with dedup transparency (bulk-fetched → tier-gated → cross-run-dedup → uploaded).
4. **DataSift Uploads** — one line per CSV that landed.

The consolidated post is titled "SiftStack Daily Sweep". The earlier APN-only post is titled "APN Scrape — early progress" with a subtitle pointing to the consolidated post.

### Workflow wiring — `.github/workflows/daily-sweep.yml`

Each source is a separate step. Order matters only for the state-file `git add` at the end. All 3 trustee-portal steps (RL, T&B pending, HWM) MUST include `--skip-trace` (5c34efa fix — earlier versions omitted this and phones stayed empty).

### Recovery procedures

| Symptom | Cause / Fix |
|---|---|
| Every notice detail page fails CAPTCHA | Check 2Captcha balance at 2captcha.com. Turnstile sitekey lives in `src/config.py`. See `SESSION_2026-07-02.md` for the reCAPTCHA→Turnstile migration playbook. |
| Smarty returns "subscription error" or 401 | Verify `us-core-cloud` license active at smartystreets.com. Test locally: `python -c "from src.address_standardizer import _build_client; c=_build_client(); print(c)"`. If local .env works but GHA fails, GHA secrets are stale — rotate via `gh secret set SMARTY_AUTH_ID` + `gh secret set SMARTY_AUTH_TOKEN` (stdin, no history). |
| One trustee source suddenly returns 0 target-county records | HTML structure likely changed (esp. T&B — ASP.NET IDs are auto-generated). Curl the portal manually, compare against adapter's parser selectors. RL/HWM use static HTML → most likely a class rename. |
| Slack posts show numbers but DataSift shows 0 uploads | Check `output/leads/` for the CSV files — they exist locally in the GHA workspace even if the DataSift upload step failed. Look at the "DataSift Uploads" section: missing rows = the upload step logged an error. Check the workflow log for `datasift_uploader.py` output. |
| `notice_parser` LLM tiebreaker silently no-ops | Verify `is_target_county_async()` signature accepts `rate_tracker` kwarg (3999bec fixed this — was silently NameError-ing on every call before). |
| AL notice's ZIP shows as None in main.py daily | Regression in `ZIP_RE` — must accept `3[5-8]\d{3}` (c570b65). TN-only regex would silently reject AL. |
| New adapter added but Slack Breakdown doesn't show it | Add source label to `_foreclosure_source_label()` in `scripts/daily_finalize.py` (d63d531). Otherwise it defaults to "Other" and rolls up incorrectly. |

## Alabama Probate Pipeline (Jefferson + Madison)

Probate "Notice to Creditors" publications on alabamapublicnotices.com follow a tightly templated format mandated by Alabama Code § 43-2-61 (publication for 3 successive weeks) and § 43-2-350 (creditors have **6 months** from grant of letters to file claims). The notice body always contains a case number, the Judge of Probate's name, and the date Letters Testamentary or Letters of Administration were granted — but **never the decedent's property address**. Filling that gap is the second half of the pipeline.

### Notice metadata extraction (regex + LLM)

| Field | Source | `NoticeData` slot |
|---|---|---|
| Case # (e.g. `PC2025-234`, `PR-2026-000557`) | `CASE_NUMBER_RE` on top-of-document field | `case_number` |
| Decedent name | Existing `DECEDENT_NAME_RE` (`"Estate of [NAME], Deceased"`) | `decedent_name` |
| PR / Executor / Administrator | `PROBATE_NAME_RE` — fragile on AL "name-before-title" format; LLM fallback recommended | `owner_name` |
| Judge of Probate | `JUDGE_RE` (`"Honorable {Name}, Judge of Probate"` or `"Hon. {Name}"`) | `judge_name` |
| Granted date | `GRANTED_DATE_RE` (handles both `"the X day of MONTH, YEAR"` and `"MONTH X, YEAR"`); falls back to top-of-document recording stamp | `granted_date` |
| Creditor deadline | Computed: `granted_date + 6 months` (per § 43-2-350) | `creditor_deadline` |
| PR mailing address | Existing `PR_ADDRESS_RE` (now bistate TN/AL via `notice.state`) | `owner_street` / `owner_city` / `owner_state` / `owner_zip` |

`_parse_probate_metadata(notice)` runs after regex extraction; the LLM (`llm_parser.py`) backfills `case_number`, `judge_name`, `granted_date` when regex misses. A second `_parse_probate_metadata` pass runs after the LLM fills `granted_date` so `creditor_deadline` gets recomputed.

### Property-address enrichment (probate → parcel)

The notice gives us a name; the tax roll gives us the address. Adapters wrap each county's public property search:

| County | Tool | Module | URL | Auth | Returns |
|---|---|---|---|---|---|
| **Madison** | AssuranceWeb (countygovservices.com) | [src/madison_property_api.py](src/madison_property_api.py) — `search_by_owner_name()` | [madisonproperty.countygovservices.com](https://madisonproperty.countygovservices.com/Property/Property/Search) | None (CSRF token grabbed from form) | Parcel #, situs address, owner name, total tax, balance due, delinquent flag |
| **Jefferson** | E-Ring Capture API (underlying the SPA) | [src/jefferson_property_api.py](src/jefferson_property_api.py) — `search_by_owner_name()` | `POST jeffersonexpress.capturecama.com/SearchRP` | None | Parcel #, situs + mailing address, owner name, valuation, tax lien count, delinquent flag, exemption code |

**Madison adapter** — `httpx`, no Playwright:
1. `GET /Property/Property/Search` → captures session cookie + `__RequestVerificationToken`
2. `POST /Property/Property/Search` with `PropertySearchType=name`, `SearchCriteria.Criteria1=<LAST>`, `SearchCriteria.Criteria2=<FIRST>`, plus the CSRF token
3. Response is HTML containing the Kendo Grid initialization with the entire result set inlined as JSON record objects
4. Regex out each record (`{"Selected":false,"ParcelInfoID":...,"PropertyType":"Real"}`) and `json.loads` it

**Jefferson adapter** — `httpx`, no Playwright (the Incapsula-protected SPA front door is bypassable because the underlying REST API is open):
1. `POST https://jeffersonexpress.capturecama.com/SearchRP` with JSON body `{tenantUrl, expressUrl, reserved, searchstring, searchtype: "1", recordyear}`
2. Response is a JSON array of `MigratedOwners` records; map directly to `JeffersonPropertyRecord`
3. **Two gotchas**:
   - Jefferson's `MigratedOwners` field uses a **double space** between surname and given name (`"SMITH  OPAL W"`); a single-spaced multi-token query returns 0 results. The adapter normalizes by inserting the second space after the first token automatically.
   - The capturecama.com host serves an **incomplete SSL chain** — Python's `certifi` doesn't have the intermediate. The adapter sets `verify=False` with a comment; browsers and curl-system trust handle it transparently via OS keychain.

**Decedent-flagged records on the tax rolls** — both county assessors track owner-of-record status, but with **different conventions**:
| Marker | Madison | Jefferson |
|---|---|---|
| Generic deceased | `(HEIRS OF)`, `(ESTATE OF)` | `(D)` |
| Joint owner deceased | `LIFE ESTATE AND ... REMAINDER` | `& X (D)` or `X (D) & Y` |
| Heir-managed | `X HEIRS OF` | `X AGT FOR HEIRS OF`, `X AGT OF HEIRS FOR Y` |

These markers are the highest-confidence signal in the property-match scoring (`_DECEASED_MARKERS` in `probate_property_locator.py` includes all of them).

### Probate property locator orchestrator

[src/probate_property_locator.py](src/probate_property_locator.py) chains the two adapters into a single decision-maker for probate notices:

```python
from probate_property_locator import enrich_notice_with_property
enrich_notice_with_property(notice)  # returns True if a match was applied
```

Lookup waterfall:
1. **Tier 1 — decedent-name search** against the county adapter (`notice.county` → Jefferson or Madison). All records scored by token-overlap in `_score()`, with +0.2 bonus if any deceased marker appears in the recorded owner name.
2. **Tier 2 — PR-name search** if Tier 1 returns nothing above `min_score=0.5`. Catches family property where the deceased was a joint owner with the PR (typical: surviving spouse becomes PR).
3. **Tiers 3 & 4** (people-search waterfall, Tracerfy skip trace) are intentionally NOT in this module — they're already separate modules in the enrichment pipeline and chain downstream of this one.

**Multi-parcel return.** `find_probate_properties()` (plural) returns a `ProbateMatchSet` containing the primary residence plus all additional parcels owned by the same decedent — common for estates with a homestead + rental + family land. Primary-residence selection priority: `(is_homestead AND deceased_flagged) > is_homestead > total_value > score`. Both adapters expose an `is_homestead` flag computed from county-specific signals:

| County | Homestead heuristic |
|---|---|
| Jefferson | `improvement_value > 0` AND mailing-address == situs-address AND (non-empty `ExmtCode` OR `improvement_value > $10K`) — owner-occupied with a structure |
| Madison | `is_buildable` (situs has a non-zero house number — drops obvious vacant lots like "0 STREET") |

`enrich_notice_with_property()` writes the rollup onto the notice: `address` / `city` / `state` / `zip` / `parcel_id` / `tax_owner_name` / `is_homestead` for the primary, plus pipe-delimited `secondary_addresses` and `total_estate_value` (sum of all matched parcels' total_value) for the multi-parcel summary. CLI for ad-hoc testing:

```bash
python src/probate_property_locator.py Madison "FULENWIDER ORVELENE"
python src/probate_property_locator.py Jefferson "SMITH OPAL W"
```

### AL probate PR-name extraction (multi-pattern)

`_parse_name()` for probate notices tries three patterns in priority order, validating each via `_is_valid_name` (which rejects junk like `"the undersigned"`):

1. **`PROBATE_NAME_GRANTED_RE`** — matches `"having been granted to NAME on/as..."` (most reliable for AL prose, but rejects "the undersigned")
2. **`PROBATE_NAME_BEFORE_TITLE_RE`** — matches the AL signature-block format `"NAME\nPersonal Representative"` or `"NAME, Executor"`
3. **`PROBATE_NAME_RE`** — original TN-style `"Personal Representative: NAME"` (kept for backward compat)

This fixed the prior bug where AL notices captured `"Of The Estate"` or `"Letters Testamentary Under The"` as `owner_name` because none of the legacy TN patterns matched.

### Probate notice subtypes

`_parse_probate_subtype()` runs after the standard probate metadata extraction and assigns one of three mutually-exclusive `notice_subtype` values. Only one subtype matches per notice — they're checked in priority order.

| Subtype | Trigger | Extra fields populated |
|---|---|---|
| `probate_sale` | `PROBATE_SALE_SIGNATURE_RE` matches `"NOTICE OF SALE OF REAL PROPERTY"`, `"PETITION TO APPROVE SALE OF REAL PROPERTY"`, or `"NOTICE OF SALE OF REAL ESTATE BY (THE) PERSONAL REPRESENTATIVE"` | `petition_filed_date`, `hearing_date`, `estate_purpose`, `sale_type`, `co_pr_names` |
| `probate_heirs_notice` | `PROBATE_HEIRS_NOTICE_RE` matches `"NOTICE TO: NAME1, NAME2..."` (2+ comma-separated all-caps names) | `heirs_named_in_notice` (pipe-delimited; max 10 names; "to whom it may concern" / "next of kin" filtered out) |
| `probate_creditors` | default — assigned when neither of the above matches | (just the standard probate metadata: `case_number`, `judge_name`, `granted_date`, `creditor_deadline`, `decedent_name`, `owner_name`) |

Co-PR detection runs independently across all three subtypes: if `CO_PR_FLAG_RE` matches `"Co-Personal Representatives"` / `"Co-Executors"` etc., a follow-up regex captures `"NAME1 and NAME2, Co-PR"` patterns into `co_pr_names` (pipe-delimited).

**Why `probate_sale` matters most for deal flow**: the PR has explicitly decided to sell — they're advertising. The hearing date is the court-approval deadline; making an offer before the hearing closes lets you avoid MLS competition. Filter the DataSift list on `notice_subtype = "probate_sale"` AND `hearing_date` in the next 30 days for a high-touch sequence.

### CSV column coverage (probate export schema)

The CSV exporter ([data_formatter.py](src/data_formatter.py)) writes one row per notice with all fields from the AL probate-export schema. Fields populated by stage:

| Stage | Fields populated |
|---|---|
| Search-results page | `received_date` (scrape timestamp), `date_added` (publication date), `notice_type`, `county`, `source_url` |
| Detail page + CAPTCHA + parsers | `case_number`, `judge_name`, `granted_date`, `creditor_deadline`, `decedent_name` (+ `decedent_first/middle/last/suffix`), `owner_name` (+ `owner_first/middle/last/suffix`), `owner_street/city/state/zip` (PR mailing address), `notice_subtype`, `petition_filed_date`, `hearing_date`, `co_pr_names`, `heirs_named_in_notice`, `estate_purpose`, `sale_type` |
| Property locator | `address` / `city` / `state` / `zip`, `parcel_id`, `tax_owner_name`, `is_homestead`, `assessed_value` (primary parcel), `property_use` (assessor classification), `secondary_addresses`, `total_estate_value` |
| Zillow enrichment (downstream) | `bedrooms`, `bathrooms`, `year_built`, `mls_status`, `estimated_value`, etc. |
| Obituary / Tracerfy (optional, paid) | `survivor_zip` (auto-fills from `decision_maker_zip` when DM is distinct from PR; otherwise empty for external skip-trace fill) |
| CSV exporter | `S No` (row counter, 1-indexed) |

**Name splits** are produced by `_split_full_name()` which handles "Mary Angela Caylor Roling" → first/middle="Angela Caylor"/last/suffix and "James F. Smith Jr." → first/middle="F."/last/suffix="Jr". Joint owners ("John Doe and Jane Doe") use only the first listed person.

**Decedent-name orientation**: probate notices write "FIRST MIDDLE LAST" but the Jefferson tax roll stores "LAST FIRST MIDDLE". The locator's Jefferson adapter automatically retries with last-first reordering if the original query returns nothing — the caller can pass either format and get the same result.

**Property classification**: `property_use` reads "Residential" / "Commercial" / "Utility" / "Vehicle" / "Other" for Jefferson (mapped from `AssmtClass` codes 1–4) and "Real Property" / "Personal" for Madison (which doesn't expose finer classification in the search response — would require fetching each parcel-detail page).

## Alabama Post-Probate Pipeline (Jefferson Benchmark)

**Independent, second probate pipeline.** While `## Alabama Probate Pipeline` (above) scrapes APN newspaper-published Notice-to-Creditors publications, this pipeline reads cases directly from Jefferson County Probate Court's case management system at [benchmarkweb.jccal.org](https://benchmarkweb.jccal.org) (login required, replicating the data source RealSupermarket's vendor uses). It covers the full Jefferson probate case stream — far more comprehensive than what publishes in newspapers — and runs an aggressive ZIP-tier filter so we only spend enrichment dollars on properties in our target investment ZIPs.

**Madison is NOT supported** — Madison Probate doesn't have a public Benchmark equivalent.

### Pipeline flow

```
Benchmark Web case list (JS app, login-required)
   ↓ src/benchmark_web.py — Playwright async, parses parties accordion individually (clicking all at once redirects to InvalidCase)
Per case: Jefferson property API search by decedent name
   ↓ src/probate_property_locator._search_jefferson — handles last-first reorder + middle-name truncation fallbacks
ZIP gate: parcel.situs_zip in Tier 1 ∪ Tier 2?
   ↓ src/target_zips.py — see "Tier definitions" below
   ↓ Drop OFF-target (e.g. luxury 35243, sub-$125K 35211) and NO-PROPERTY cases entirely
Fiduciary detection: petitioner appears in 2+ cases this batch?
   ↓ Skip obituary spend on attorneys/public administrators
Obituary cross-reference (DDG → Ancestry/Newspapers fallback)
   ↓ src/benchmark_obituary_match.py — single Claude Haiku call returns the full family graph
   ↓ Extracts: petitioner-as-survivor match, decedent age/DOD/city, all_survivors, spouse, predeceased, executor
   ↓ rank_decision_makers() ranks by AL signing-authority law (executor > spouse > children > siblings)
PR fallback when obit fails
   ↓ Use court-appointed petitioner as DM with confidence=medium, source="benchmark_court_record"
Tracerfy batch skip-trace
   ↓ src/tracerfy_skip_tracer — fills phones/emails for DM #1 and signing-authority heirs (up to 5 per property)
Heir-phone promotion to NoticeData phone slots
   ↓ Heir phones land in heir_map_json by default; this step surfaces them into Phone 1-9 / Email 1-5
DataSift CSV (80 cols) + Slack notification
   ↓ src/datasift_formatter.write_datasift_csv — same formatter the legacy scraper uses
   ↓ Slack: per-lead action card (address, value, DM, confidence, obit link, phone status)
```

### Entry point + CLI

```bash
# Daily ops (recommended invocation)
python src/benchmark_pipeline_al.py --days-back 14 --tiers 1,2 --datasift-csv --skip-trace --notify-slack

# Diagnostic flags
python src/benchmark_pipeline_al.py --days-back 7 --no-obituary       # ZIP gate only — fastest
python src/benchmark_pipeline_al.py --days-back 14 --headed            # show Benchmark browser
python src/benchmark_pipeline_al.py --days-back 14 --json              # JSON output for downstream tools
python src/benchmark_pipeline_al.py --days-back 14 --tiers 1           # Tier 1 only
python src/benchmark_pipeline_al.py --ancestry-fallback                # opt-in obituary gap-closer (see below)
```

### Key modules

| Module | Role |
|---|---|
| [src/benchmark_web.py](src/benchmark_web.py) | Async Playwright adapter — login, search by Clerk-Filed-Date range, parse case detail (parties + dockets) |
| [src/benchmark_obituary_match.py](src/benchmark_obituary_match.py) | DDG obituary search + Claude Haiku family-graph extraction; Ancestry/Newspapers fallback with 3-year DOD sanity gate |
| [src/target_zips.py](src/target_zips.py) | Frozensets of Tier 1/Tier 2 ZIPs + `zip_tier_county()` helper |
| [src/benchmark_pipeline_al.py](src/benchmark_pipeline_al.py) | Orchestrator — fetch, filter, enrich, write CSV, post Slack |

### Tier definitions

Source-of-truth analysis docs live outside the repo at:
- `~/Documents/Claude/Projects/REI Skill Library/Jefferson_County_AL_SFR_125K_500K_Market_Analysis.md`
- `~/Documents/Claude/Projects/REI Skill Library/Madison_County_AL_SFR_Market_Analysis.md`
- `~/Documents/Claude/Projects/REI Skill Library/Marshall_County_AL_SFR_125K_500K_Market_Analysis.md`

Codified in [src/target_zips.py](src/target_zips.py). **All three must be kept in sync** — the codified set drives the filter; the MD docs explain why each ZIP is on the list.

| Tier | Jefferson | Madison | Marshall |
|---|---|---|---|
| **Tier 1** | 35215, 35214, 35022, 35023, 35226, 35235 | 35810, 35811, 35803, 35758, 35805, 35801 | 35950, 35976, 35016, 35961, 35951, 35957 |
| **Tier 2** | 35216, 35126, 35210, 35173, 35244 | 35757, 35759, 35763, 35806, 35750 | 35962, 35175, 35747, 35769, 35980 |

33 priority ZIPs total (18 T1 + 15 T2). Note: Benchmark itself only covers Jefferson — Madison + Marshall ZIPs are gates for the other pipelines (pre-probate, APN probate, tax-distress, distress-proxy).

### Obituary extraction — what comes back

The single LLM call (Claude Haiku, ~1500 tokens) returns the full family graph from the obituary, not just "is this the right person":

| Field | Notes |
|---|---|
| `is_decedent_match` / `decedent_full_name` / `decedent_city` / `decedent_state` / `decedent_age_at_death` / `date_of_death` / `decedent_obit_address` | Decedent identification + DOD sanity inputs |
| `petitioner_match` (`exact`/`fuzzy`/`not_found`) / `petitioner_survivor_name` / `petitioner_relationship` / `petitioner_city` | Cross-reference of court-named PR against obituary survivors |
| `all_survivors` (array of `{name, relationship, city}`) | Full family graph |
| `spouse_name` | Surviving spouse — usually the highest-priority DM under AL signing-authority law |
| `preceded_in_death` | Family who died first — relevant for narrowing heirship |
| `executor_named` | Only if the obituary explicitly names the executor (rare) |

`rank_decision_makers()` (in `obituary_enricher.py`) consumes `all_survivors` + `executor_named` and produces a ranked list with `signing_authority` flags per AL intestate succession law.

**Bug fixed during build**: the spouse-of-relative pattern (`"spouse of son"`, `"husband of daughter"`) was previously misclassifying daughters/sons-in-law as having signing authority. Fix in `_spouse_of_patterns` extends the set with prepositional forms.

### Ancestry/Newspapers fallback (`--ancestry-fallback`)

When DDG returns `confidence=none` (no obituary found, or found but petitioner isn't in survivors), the orchestrator can fall through to a batch Ancestry pass via [src/ancestry_enricher.py](src/ancestry_enricher.py):

1. Single shared browser session for all gap cases
2. SSDI → Ancestry obituary collection → Newspapers.com cascade
3. **3-year DOD sanity gate** — rejects matches whose death predates the probate filing by >3 years (catches SSDI name-collision false positives like "William Belew died 2008" matching a 2026 probate)
4. If a confirmed obituary URL is recovered, fetch text and run the SAME LLM extraction the DDG path uses

**One-time setup required** — Ancestry's auto-login can't get past CAPTCHA/MFA on first launch. Run a one-time interactive bootstrap to seed `.ancestry_profile/Default/` cookies; subsequent runs reuse the session via persistent context. Bootstrap script pattern lives in conversation history; equivalent: open Ancestry signin in the persistent profile, log in by hand, save cookies.

### Tracerfy + heir-phone promotion

[src/tracerfy_skip_tracer.batch_skip_trace()](src/tracerfy_skip_tracer.py) runs the existing Tracerfy batch endpoint over the prepared NoticeData list. It already populates `primary_phone`/`mobile_1-5`/`landline_1-3`/`email_1-5` for DM #1 and stores phones for non-DM-#1 signing heirs in `heir_map_json` per heir.

**Heir-phone promotion** (in `benchmark_pipeline_al._promote_heir_contacts_to_csv_slots`): walks `heir_map_json` after Tracerfy runs and fills any empty Phone N / Email N slots from heir entries (deduping). Without this pass, heir phones live only in `heir_map_json` and never appear in the CSV columns DataSift filter presets read.

Cost: ~$0.02 per contact (≈$0.06–$0.30 per typical batch). Realistic match rate: 1/3 contacts in a 3-contact test (Tracerfy is name+address dependent; nicknames like "Dede" hurt recall).

### Slack notification

`benchmark_pipeline_al.notify_slack()` posts via [src/slack_notifier._send_webhook](src/slack_notifier.py) (uses `SLACK_WEBHOOK_URL`). Message includes per-lead action cards: address, tier, value, DM name + relationship + confidence, decedent age/DOD, obituary URL, skip-trace stats, CSV filename. Same webhook the legacy `main.py daily --notify-slack` uses; both pipelines coexist on the same channel.

### Realistic conversion rates

From a 14-day window across May 2026 sample runs:
- **In-tier conversion**: ~15-20% of pulled cases survive the ZIP gate
- **Obituary confirmation**: ~50-60% of in-tier cases get an obituary-confirmed DM
- **Skip-trace match**: ~30% of contacts return phones (Tracerfy)
- **Net**: typical 14-day batch yields 1-3 fully-enriched leads (DM identified, phone attached) plus 1-3 PR-fallback leads (court PR, no obit corroboration but mailable)

### What's NOT done

- **Daily scheduler / cron** — pipeline must be invoked manually today
- **Madison post-probate** — no Benchmark equivalent for Madison; would require a separate adapter
- **Vacancy enrichment** — knowing the inherited property is actually unoccupied (USPS NCOA, water shutoff) would tighten the lead score further

## Alabama APN Post-Probate Pipeline (Jefferson + Madison, newspaper-driven)

**The canonical Madison post-probate path.** Where `benchmark_pipeline_al.py` pulls live Jefferson court records, this orchestrator pulls the same shape of data from the alabamapublicnotices.com newspaper publications — the only public Madison probate source (Madison's online portal at `madisonprobate.countygovservices.com` is recording-only, parallel to Jefferson's Landmark Web; it has no case-management view). Jefferson runs through both Benchmark AND APN; the duplication is fine because Benchmark covers the full case stream while APN covers only what gets formally published.

### Pipeline flow

```
Scrape APN for Jefferson + Madison probate Notice-to-Creditors publications
   ↓ src/scraper.py — ASP.NET WebForms, reCAPTCHA v2 per detail page (~$0.003/notice via 2Captcha)
   ↓ Existing seen_ids.json deduplicates across runs (daily cadence ≈ 5-15 new notices/day)
For each notice: enrich_notice_with_property() (probate_property_locator)
   ↓ Tier-1 search by decedent name in the matching county API
   ↓ Tier-2 fallback: search by PR (petitioner) name
Madison-only: one-shot Smarty geocode to recover missing ZIP
   ↓ Madison's name-search response lacks situs_zip; same fix as pre-probate
ZIP gate: parcel.situs_zip in Tier 1 ∪ Tier 2?
   ↓ Same target_zips.zip_tier_county() used by the other pipelines
For survivors: notice.notice_type='probate' is already populated
   ↓ Tracerfy skip-trace (DM #1 + signing-authority heirs)
   ↓ Heir-phone promotion to NoticeData phone slots
DataSift CSV (Lists="Probate") + Slack notification
```

### Entry point + CLI

```bash
# Daily ops (recommended)
python src/apn_probate_pipeline_al.py --counties Jefferson,Madison \
    --days-back 7 --tiers 1,2 --datasift-csv --skip-trace --notify-slack

# Single county / smaller window
python src/apn_probate_pipeline_al.py --counties Madison --days-back 3 --max-notices 30
```

### Key modules

| Module | Role |
|---|---|
| [src/apn_probate_pipeline_al.py](src/apn_probate_pipeline_al.py) | Orchestrator. Filters `SAVED_SEARCHES` to probate-only for the chosen counties, calls `scraper.scrape_all()`, runs property locator + ZIP gate, reuses pre-probate's heir-phone promotion. |
| Reuses [src/scraper.py](src/scraper.py) | The existing APN scraper (no changes needed) |
| Reuses [src/probate_property_locator.py](src/probate_property_locator.py) | `enrich_notice_with_property()` — same Tier 1/2 search waterfall used by the legacy `main.py daily` flow |
| Reuses [src/pre_probate_pipeline_al.py](src/pre_probate_pipeline_al.py) | `_smarty_zip_for_madison_address()` (Madison ZIP recovery) and `_promote_heir_contacts_to_csv_slots()` |

### Why this is the canonical Madison post-probate

**Madison's online probate portal is recording-only** — `madisonprobate.countygovservices.com` (free signup, Azure AD B2C) exposes 14 search categories (DEEDS, MORTGAGES, JUDGMENTS, UCC, MISCELLANEOUS, TAX SALES, MARRIAGE LICENSES, BONDS, PLATS, MILITARY DISCHARGES, etc.) but **NO** "Estate Cases" / "Letters Testamentary" / "Probate Cases" category. The result table headers are recording-style: `Book / Book # / First Page # / Name / Class / Other Names / Instrument / Legal Description / Recorded On / Pages`. There's no case-management view (no parties, no docket, no judge field).

So Madison post-probate must use APN newspaper publications. The flow is structurally similar to Jefferson's APN flow but Madison-specific:
1. Madison publishers (Madison County Record, Speakin' Out News, etc.) publish image-PDF Notice-to-Creditors
2. The existing scraper handles the Madison PDF format (text-layer pdfminer + OCR fallback)
3. `_normalize_pdf_text()` de-hyphenates column-wrapped words from newspaper layout

### What's NOT done

- **Bonds-as-LT-proxy adapter** — searching `madisonprobate.countygovservices.com`'s "Probate Bonds" category by date range would catch newly-appointed PRs (most non-spousal probates require a bond). Lower-fidelity than APN (no case number, no published Letters Testamentary text) but catches cases that don't formally publish a Notice-to-Creditors. ~3-4 hr build. Defer until APN volume proves insufficient.
- **Daily scheduler / cron** — same as the other pipelines, manual invocation only
- **Cost monitoring** — APN scraping costs CAPTCHA solves (~$0.003/notice). With `seen_ids.json` deduplicating across runs, daily cadence settles to ~5-15 new notices/day = ~$0.05/day. First-run on a fresh `seen_ids.json` will burn ~$3-5 because it processes the full backlog.

## Alabama Pre-Probate Pipeline (Jefferson + Madison, obituary-driven)

**Companion to the Post-Probate pipeline.** Where post-probate reads court records (case-driven, 30-90 days after death), pre-probate works backwards from fresh obituaries (death-driven, days fresh). Same downstream: ZIP gate → enrichment → DataSift CSV → Slack. Together both pipelines replicate the full RealSupermarket vendor product the user was buying.

**Sources**:
- legacy.com Birmingham listing (`legacy.com/us/obituaries/local/alabama/birmingham`) → Jefferson property API
- legacy.com Huntsville listing (`legacy.com/us/obituaries/local/alabama/huntsville`) → Madison property API

The harvester pulls both by default and tags each `HarvestedObit` with a `county_hint` so the orchestrator routes property lookups to the correct county API first. If the hinted county returns nothing, it falls back to the other county (catches cross-county property ownership — decedent died in Birmingham hospital but owned property in Huntsville, etc.).

**URL formats** (the Birmingham and Huntsville listings use different patterns and the harvester handles all three):
- `legacy.com/person/<slug>-<id>` — Birmingham preview pages (need URL upgrade to obits.al.com for full text)
- `legacy.com/us/obituaries/{birmingham,huntsville,vnews}/name/<slug>-obituary?id=<id>` — Huntsville's primary format AND Birmingham's syndicated entries; render the full obituary inline (no upgrade needed)
- `obits.al.com/us/obituaries/{birmingham,huntsville}/name/<slug>-obituary?id=<id>` — partner-syndication URLs

### Pipeline flow

```
Harvest obit URLs from BOTH Birmingham + Huntsville listings (paginated)
   ↓ src/obituary_harvester.py — Firecrawl-rendered, dedupes by (source, ID)
   ↓ Each HarvestedObit tagged with county_hint = "Jefferson" | "Madison"
For each obit URL:
   ↓ Fetch obit text — auto-upgrade legacy.com/person/* → obits.al.com/* (Birmingham only)
   ↓ Cross-source dedupe: skip if effective URL already processed (legacy + alcom URLs converge)
LLM extract decedent + family graph (Claude Haiku, ~1500 tokens)
   ↓ DECEDENT_PROMPT — accepts preview-only obits, returns name+DOD+age at minimum
DoD freshness gate: drop obits >2 years old (legacy listing occasionally surfaces stale syndicated entries)
County-routed property API search by decedent name
   ↓ Hinted county first (Jefferson for Birmingham obit, Madison for Huntsville obit)
   ↓ Falls back to the OTHER county for cross-county ownership cases
   ↓ Madison records lack ZIP/city (search response only returns street) — one-shot Smarty geocode fills both
ZIP gate: parcel.situs_zip in Tier 1 ∪ Tier 2 (across Jefferson + Madison ZIPs)?
   ↓ target_zips.zip_tier_county() handles both counties' tier ZIPs
For survivors: rank_decision_makers (executor > spouse > children > siblings)
   ↓ No court-PR fallback (there is no court case yet)
Tracerfy skip-trace (DM #1 + signing-authority heirs, up to 5)
   ↓ Heir-phone promotion fills empty Phone N / Email N slots
DataSift CSV with Lists="Pre-Probate/Deceased" + Slack notification
   ↓ notice.county set dynamically from matched_county (jefferson | madison)
```

### Entry point + CLI

```bash
# Daily ops (recommended invocation — both counties)
python src/pre_probate_pipeline_al.py --markets Birmingham,Huntsville --limit 50 --pages 2 \
    --tiers 1,2 --datasift-csv --skip-trace --notify-slack

# Single-market runs
python src/pre_probate_pipeline_al.py --markets Birmingham --limit 50  # Jefferson only
python src/pre_probate_pipeline_al.py --markets Huntsville --limit 50  # Madison only

# Diagnostic flags
python src/pre_probate_pipeline_al.py --limit 25                  # quick dry run, no CSV
python src/pre_probate_pipeline_al.py --limit 50 --tiers 1        # Tier 1 only
python src/pre_probate_pipeline_al.py --limit 50 --json           # JSON output
```

### Key modules

| Module | Role |
|---|---|
| [src/obituary_harvester.py](src/obituary_harvester.py) | Pulls fresh obit URLs from BOTH `legacy.com/us/obituaries/local/alabama/birmingham` AND `.../huntsville`. Walks N listing pages, dedupes by (source, ID), tags each `HarvestedObit` with `county_hint`. Public API: `harvest_alabama(markets)` returns `list[HarvestedObit]`. |
| [src/pre_probate_pipeline_al.py](src/pre_probate_pipeline_al.py) | Orchestrator. Has `_fetch_full_obit_text()` URL-upgrade helper, `DECEDENT_PROMPT`, county-routing in `_attach_property_for_decedent()`, `_smarty_zip_for_madison_address()` for Madison's missing ZIP, `_to_notice_data()`, `prepare_notices()`, `notify_slack()`. |

### URL upgrade — the critical fix

Legacy.com person pages (e.g., `legacy.com/person/Jane-Doe-12345678`) **always** show only a preview snippet with a "Read the full obituary on our trusted partner sites below" link to obits.al.com. The actual extractable obituary text lives on obits.al.com. The `_fetch_full_obit_text()` helper auto-follows this cross-reference: if the URL is `legacy.com/person/...` AND the page contains an `obits.al.com/.../obituary?id=...` link, fetch THAT URL and use its text instead.

Without this upgrade, ~80% of obits get rejected as "not an obituary" by the LLM because all it sees is the legacy.com preview chrome. With it, ~30-40% still drop (alt partner sites like dignitymemorial, individual funeral homes — TODO to wire those too).

### Cross-source dedupe

The harvester pulls BOTH legacy.com person URLs AND obits.al.com URLs from the listing — the same decedent typically appears under both. Naive processing produces 2 enriched rows per person (one through each URL path). The orchestrator's `seen_effective: set[str]` tracks effective URLs after upgrade and skips duplicates. Counted as `dropped_duplicate` in the run summary.

### Decision-maker logic vs post-probate

Same `rank_decision_makers()` from `obituary_enricher.py` — outputs ranked DMs by AL signing-authority law (executor > spouse > children > siblings > others). **Difference**: pre-probate has NO court-appointed PR yet, so there's no PR-fallback when LLM extraction fails. If `rank_decision_makers` returns empty, `dm_confidence = "low"` and `missing_data_flags = "no_decision_maker"` — these are flagged for manual research rather than auto-populated.

### Realistic conversion rates

From the 2026-05-06 47-obit run (Birmingham listing pages 1-3):
- 47 obits harvested, 9 cross-source duplicates skipped
- ~30% had findable Jefferson property
- ~4% landed in Tier 1/Tier 2 ZIPs (2 of 47)
- 50% of in-tier leads got Tracerfy phones (1 of 2)
- **Per-day yield**: 1-3 enriched leads (best case)
- **Per-run cost**: ~$0.06-0.30 (Firecrawl listing/page fetches + Tracerfy contacts)

### Lists / Tags / DataSift

Setting `notice_type = "pre_probate"` causes the DataSift formatter to populate:
- **Lists**: `Pre-Probate/Deceased` (per `NOTICE_TYPE_TO_LIST` mapping in `datasift_formatter.py`)
- **Tags**: `Courthouse Data, pre_probate, jefferson, YYYY-MM, deceased, medium_confidence, has_heirs, signing_chain_N, municipality_<city>, homestead`
- All 80 columns of the standard DataSift schema (same as post-probate)

### Property-search fallback paths (lift recall from ~67% → ~92%)

`_attach_property_for_decedent()` tries three search paths in order. The first that returns a valid match wins:

1. **By decedent name** (primary) — searches both county APIs (hinted county first), uses `_search_jefferson` / `_search_madison` with their internal last-first reorder + middle-name truncation fallbacks. Catches the common case where the decedent is on the title.
2. **By obit-stated address** (fallback A) — when the LLM's `decedent_obit_address` is populated AND Path 1 returned nothing, searches by situs address via `jefferson_property_api.search_by_situs_address()`. Recovers cases where the property is held in a trust, LLC, or spouse's name (so it's NOT findable by the decedent's name) but the address itself is on the current tax roll. Recall lift verified at **+16 pp** in the May 2026 ground-truth check against the RealSupermarket CSV.
3. **By spouse name** (fallback B) — when Paths 1+2 both miss AND the LLM extracted `spouse_name`, searches by spouse's name and validates by checking the decedent's last name appears in the matched property's `owner_name` (proves it's the family property, not a same-named stranger). Catches the "surviving spouse stays in the home, decedent never went on title" pattern.

The fallbacks fire in priority order — Paths 2 and 3 only run when the prior path returned no valid match. Logs them as `[addr-fallback]` and `[spouse-fallback]` for audit-trail visibility.

### Ground-truth recall vs RealSupermarket (Apr 2026 dump)

The `~/Documents/Claude/Projects/REI Skill Library/REISift_Upload_Jefferson_Pre-Probate.csv` contains 2,438 Jefferson pre-probate records RealSupermarket sold the user in April 2026. Used as ground truth for our recall measurement:

| Metric | Value | Note |
|---|---|---|
| Total RS records | 2,438 | Whole Jefferson County (no tier filter) |
| In our Tier 1 ZIPs | 654 (26.8%) | All 6 T1 ZIPs hit, top is 35023 Hueytown (189) |
| In our Tier 2 ZIPs | 348 (14.3%) | All 5 T2 ZIPs hit, top is 35173 Trussville (108) |
| Off-tier (filtered out) | 1,436 (58.9%) | Luxury 35243, sub-$125K 35211/35020/35071, etc. |
| **Our addressable pool** | **1,002 (41.1%)** | Tier 1 + Tier 2 combined |
| Recall — name only | ~67% | Pre-fallback baseline |
| **Recall — name + addr fallback** | **~92%** | After Path 2 added |
| Effective net leads vs RS | ~38% of their volume | At sub-cent per record vs vendor pricing |

The remaining 8% gap is structurally unrecoverable (property transferred / off-roll / not in current tax year).

### Madison-specific behavior

The Madison adapter (`madison_property_api.search_by_owner_name`) returns LESS data than Jefferson's E-Ring API:
- ✓ parcel_number, owner_name, situs_address (street only)
- ✓ is_buildable (used as the `is_homestead` proxy for ranking)
- ✓ is_delinquent
- ✗ situs_city, situs_zip (NOT in search response — would require per-parcel detail fetch)
- ✗ total_value (NOT in search response)
- ✗ is_homestead (use `is_buildable` instead)
- ✗ municipality (always empty)

The orchestrator handles this gap with a one-shot Smarty geocode (`_smarty_zip_for_madison_address`) per Madison match — recovers `(city, zip)` for the ZIP gate. Costs ~1 Smarty call per Madison hit.

### What's NOT done

- **Full-text partner-site fetch** — the prompt accepts legacy.com preview snippets (decedent name + age + DOD extractable from preview alone). To recover the FULL family graph for preview-only obits, we'd still need to fetch the funeral-home partner page (Welch Funeral Home, Larkin & Scott, etc.) — ~2hr fix to walk the partner links visible in the legacy page.
- **Daily scheduler / cron** — same as post-probate, manual invocation only
- **Ground-truth precision check** — comparing our pre-probate output against `~/Documents/Claude/Projects/REI Skill Library/REISift_Upload_Jefferson_Pre-Probate.csv` (the 2,438-row RealSupermarket dump from April 19) hasn't been done yet

## Alabama Tax-Delinquent + Tax-Sale Pipeline (Jefferson + Madison)

Tax sale / delinquent records flow into the same `NoticeData` schema as probate and foreclosure but the source shape is different — instead of one notice per case, the county tax portals expose **bulk lists** of all currently-delinquent parcels in a single call. APN scraping isn't worthwhile for this category; the newspaper-published tax-sale notices are giant PDF pages listing hundreds of parcels each, while the county portals serve clean structured data.

### Madison adapter — [src/madison_tax_delinquent_api.py](src/madison_tax_delinquent_api.py)

Single GET to [`/Property/Property/DelinquentParcels`](https://madisonproperty.countygovservices.com/Property/Property/DelinquentParcels) returns the entire delinquent list (~600 parcels) inlined as a Kendo Grid `"Data":[...]` JSON array. No auth, no pagination, no AJAX, no Playwright.

Parser walks balanced brackets to extract the array (legal descriptions sometimes contain `[]`), then `json.loads` it. Each `MadisonDelinquentRecord` exposes:

| Field | Notes |
|---|---|
| `parcel_id` | Formatted: "14-06-23-4-000-043.000" |
| `owner_name` | Current owner of record (`currentOwners`) |
| `previous_owner` | Most recent prior owner if changed (`previousOwners`) |
| `situs_address` | Property street address |
| `legal_description` | Full legal (subdivision, lot, plat book/page) |
| `tax_year` | Assessment year (e.g. 2025) |
| `balance_due` | Real-time balance — what's owed today |
| `tax_sale_balance` | Higher figure — what's owed at the May auction |
| `assessed_value` | County assessor's last-assessed value |
| `gross_tax`, `interest`, `other_fees`, `exempt`, `paid` | Tax breakdown |
| **`is_tax_sale_parcel`** | **boolean — Madison has already pre-flagged which delinquent parcels go to next May's auction.** This is the Phase-3 tax-sale list, embedded in the same response. |

`fetch_delinquent_parcels()` returns the full list; three filters narrow to the actionable subset:

```python
fetch_delinquent_parcels(
    tax_sale_only=False,        # Restrict to TaxSaleParcel=true (May auction subset)
    individuals_only=False,     # Drop LLC/Inc/Corp/Partnership/etc. via BUSINESS_RE
    min_balance=0.0,            # Drop records below $X owed (recommended: 5000)
)
```

Phase 1 focuses on **dollar exposure as the primary distress signal**. Madison's feed is current-year-only by design — older years are pruned after the May auction (sold) or redemption (paid off) — so timeline-based filtering doesn't apply here. A property owing $5,000+ is at meaningful risk regardless of how recently the bill became delinquent; positioning the buyer-of-choice play happens within the first-year window before the May auction forecloses the opportunity.

Each `MadisonDelinquentRecord` carries two derived flags computed at construction time:
- `is_individual_owner` — `False` when `currentOwners` matches `config.BUSINESS_RE` (LLC, Inc, Corp, Partnership, etc.). Trusts and `(HEIRS OF)` / `(ESTATE OF)` records are kept — they're personal, not commercial entities.
- `is_high_exposure` — `balance_due >= $5,000` (configurable via `HIGH_EXPOSURE_THRESHOLD`).

CLI:
```bash
python src/madison_tax_delinquent_api.py                                  # all 634
python src/madison_tax_delinquent_api.py --tax-sale-only                  # 159 (May auction subset)
python src/madison_tax_delinquent_api.py --individuals-only               # 483 (no LLCs/Incs/etc.)
python src/madison_tax_delinquent_api.py --individuals-only --min-balance 5000  # 11 high-quality leads
```

### Bulk-list → NoticeData converter

`to_notice_data(rec)` populates a `NoticeData` ready for the standard enrichment pipeline. Notice-type assignment:

| Source flag | `notice_type` |
|---|---|
| `is_tax_sale_parcel = True` | `"tax_sale"` |
| `is_tax_sale_parcel = False` | `"tax_delinquent"` |

Field mapping:
- `address` ← `situs_address`
- `owner_name` + `tax_owner_name` ← `currentOwners`
- `parcel_id` ← formatted parcel number
- `tax_delinquent_amount` ← real-time balance (string-formatted to 2 decimal places)
- `tax_delinquent_years` ← tax_year
- `assessed_value` ← `pcliVALUE` (kicks in as Estimated Value fallback when Zillow hasn't run)
- `source_url` ← deep-link to the parcel summary page (`Summary?pcliID=...&pan=...`)
- `date_added` + `received_date` ← scrape timestamp (the tax roll IS the source — there's no separate publication date)
- City/state/ZIP are intentionally left empty; Madison's delinquent feed doesn't include them and the existing Smarty enrichment step downstream fills them via address standardization.

### DataSift integration

No new columns were needed — the existing 80-column DataSift CSV already has slots for `Tax Deliquent Value`, `Tax Auction Date`, `Estimated Value`, `Parcel ID`, `Lists` (auto-maps from `notice_type` to "Tax Sale" or "Tax Delinquent").

Three new dollar-exposure tags fire automatically on tax records to enable filter-preset targeting:

| Tag | When |
|---|---|
| `tax_delinquent` | `tax_delinquent_amount > 0` (existing) |
| `tax_high_exposure` | balance ≥ $5,000 |
| `tax_high_exposure_10k` | balance ≥ $10,000 |
| `individual_owner` | `notice_type` ∈ {tax_sale, tax_delinquent} AND owner_name doesn't match `BUSINESS_RE` |
| `entity_owned` | inverse of `individual_owner` |

DataSift filter-preset compositions:

```
Madison tax delinquent — high-exposure individuals (Phase 1 canonical filter):
  notice_type:tax_delinquent AND madison AND individual_owner AND tax_high_exposure

Madison tax sale — May auction high-touch sequence:
  notice_type:tax_sale AND madison AND individual_owner

Top-tier exposure ($10k+ owed):
  notice_type:tax_delinquent AND tax_high_exposure_10k AND individual_owner
```

**Field-mapping note**: For Madison records, `tax_delinquent_years` on `NoticeData` is left empty — the feed is current-year-only and storing a misleading "0" or year value would cause confusion. The assessment year is preserved in `raw_text` for human-readable inspection. The `Tax Auction Date` built-in stays empty for now — when added, it will populate from the May tax-sale date for `tax_sale`-typed records (Phase 3).

### Jefferson adapter — [src/jefferson_tax_delinquent_api.py](src/jefferson_tax_delinquent_api.py)

The Jefferson Tax Collector publishes the official annual tax-lien auction roster on jccal.org. The two division pages ([Birmingham](https://www.jccal.org/Default.asp?ID=2663) and [Bessemer](https://www.jccal.org/Default.asp?ID=2662)) are framed announcements; the actual data lives in iframed HTML tables under `/Sites/Jefferson_County/Documents/{year}/{Birmingham|Bessemer}TaxTable-{year}.html`. The adapter fetches those tables directly (no PDF parsing needed — earlier docs anticipated PDFs but the live site uses inline HTML).

Combined volume: ~18,225 raw parcels per year (~12,794 Birmingham + ~5,431 Bessemer). Birmingham table is ~8.4 MB; the adapter uses a 120-second timeout. Single HTTP call per district, BeautifulSoup parses the table, ~3 seconds total wall time for both districts.

Each `JeffersonDelinquentRecord` exposes the 27 columns the assessor publishes:

| Field | Notes |
|---|---|
| `parcel_id` | Jefferson format: "22 00 31 3 012 003.000" |
| `lien_num` | Sequential within district |
| `district` | "Birmingham" or "Bessemer" — written through to `notice.municipality` |
| `owner_name`, `mailing_address` + `mailing_city/state/zip` | Owner of record + tax-bill mailing destination |
| `situs_address` + `situs_city/state/zip` + `situs_raw` | Property location, parsed from a single `PropertyAddress` string |
| `land_value`, `building_value`, `final_value`, `assessed_value` | Valuation breakdown |
| `tax_year`, `balance_due`, `redemption_amount`, `redemption_years` | Tax status |
| `legal_description` | Concatenated `Legal1..Legal5` |
| `is_individual_owner`, `is_high_exposure` | Same derived flags as the Madison adapter |

`fetch_delinquent_parcels(district='both', year=2024, individuals_only=False, min_balance=0.0)` mirrors the Madison API. **Important — this list IS the tax-sale roster** (not just delinquencies); the converter sets `notice_type="tax_sale"` for every record per AL § 40-10-180. CLI:

```bash
python src/jefferson_tax_delinquent_api.py                                     # ~18,225 raw
python src/jefferson_tax_delinquent_api.py --district birmingham               # ~12,794
python src/jefferson_tax_delinquent_api.py --district bessemer                 # ~5,431
python src/jefferson_tax_delinquent_api.py --individuals-only --min-balance 5000  # 310 high-quality leads
```

#### Situs-address parsing nuance

Jefferson's published `PropertyAddress` is a single string like `"426 18TH ST BHAM AL 35218"`. The adapter splits it via:
1. Tail regex matches the unambiguous `<STATE> <ZIP>` suffix.
2. Remaining text matched against a Jefferson-cities allowlist (longest-first) — `BHAM`, `BIRMINGHAM`, `BESSEMER`, `HOOVER`, `MOUNTAIN BROOK`, etc.
3. If no city matches, `city` is left empty rather than guessing — Jefferson often publishes addresses with NO city, just street + state + ZIP. The downstream Smarty step fills the city from the ZIP.

This avoids the obvious bug of treating directional suffixes (`SW`/`N`) or street types (`RD`/`DR`/`AVE`) as cities.

### Unified pipeline — [src/tax_distress_pipeline.py](src/tax_distress_pipeline.py)

Single orchestrator that runs both county adapters, converts to NoticeData, stamps auction dates (Phase 3), and optionally writes both CSV formats. The canonical daily-feed entry point.

```python
from tax_distress_pipeline import fetch_tax_distress
notices = fetch_tax_distress(
    counties=("Madison", "Jefferson"),
    individuals_only=True,
    min_balance=5000,
    stamp_auction_dates=True,   # Phase 3 — see below
)
```

CLI:
```bash
python src/tax_distress_pipeline.py --individuals-only --min-balance 5000
python src/tax_distress_pipeline.py --counties Madison
python src/tax_distress_pipeline.py --individuals-only --min-balance 5000 \
    --output-csv output/tax_distress.csv \
    --output-datasift-csv output/tax_distress_datasift.csv
```

The pipeline runs both adapters sequentially (~5 seconds total wall time), applies auction-date stamping, and prints a per-county summary. Combined Phase-1 filter output today: **321 high-exposure individual-owner records** ($2.97M total balance, $205M assessed value).

### Phase 3 — auction-date stamping

Both counties hold their annual tax-lien auctions in **early May** (per AL § 40-10-15 and the Tax Collectors' implementing rules). Specifically:
- Jefferson: Tuesday of the first full week of May (live in 2025: Tuesday May 6 per the Birmingham District announcement)
- Madison: First week of May (online via GovEase)

`next_al_tax_sale_date(today=None)` computes the next first-Tuesday-of-May on or after today. As of 2026-04-29, this returns **2026-05-05**. After May 5, 2026, it rolls forward to 2027-05-04.

`apply_auction_dates(notices)` stamps that date as `auction_date` on every notice with `notice_type="tax_sale"` that doesn't already have one. Madison records that came in as `tax_delinquent` (parcels NOT on Madison's pre-flagged auction subset) are left without an auction date — those aren't on this year's roster.

The DataSift formatter automatically maps `auction_date` to the `Tax Auction Date` column when `notice_type == "tax_sale"` (existing behavior), so DataSift filter presets like `has_auction AND tax_high_exposure AND individual_owner` work end-to-end without any further wiring.

### Status

- **Phase 1 — Madison delinquent (DONE)**: 634 records, $1.08M total balance, 159 pre-flagged for the May auction.
- **Phase 2 — Jefferson delinquent (DONE)**: 18,225 records ($2.89M total balance after individual + $5k filter, $195M property value). Direct HTML fetch — no PDF parsing needed.
- **Phase 3 — Auction-date stamping (DONE)**: `next_al_tax_sale_date()` + `apply_auction_dates()` in the unified pipeline. Stamps `Tax Auction Date = 5/5/2026` on all 312 tax_sale-flagged records (310 Jefferson + 2 Madison). `has_auction` tag fires automatically.
- **Unified orchestrator (DONE)**: `tax_distress_pipeline.py` runs both adapters in one pass with one CLI.

### Distress-Proxy Pipeline (tier-filtered tax-distress as a Huntsville-code-violation stopgap)

[src/distress_proxy_pipeline.py](src/distress_proxy_pipeline.py) — synthetic distress-proxy list for Jefferson + Madison. **Stopgap for the Huntsville code-enforcement coverage gap** (FOIA path at `docs/foia/huntsville_code_enforcement_request.md` takes weeks/months to land). Combines existing tax-distress data with absentee-owner detection + Smarty geocode + tier-ZIP gate to produce leads that overlap heavily with what code enforcement would flag (tax-delinquent + absentee = high probability of code violation).

**Layered filtering**:
1. Tax-delinquent records with `balance_due ≥ $5K` (financial distress)
2. Individual owners only (drop LLC / Corp / Partnership)
3. Madison: one-shot Smarty geocode to recover ZIP (Madison's bulk feed lacks `situs_zip`)
4. ZIP gate: keep only Tier 1 ∪ Tier 2
5. Jefferson: ABSENTEE flag when `mailing_address ≠ situs_address` (~68% of in-tier Jefferson records). Madison can't compute this (mailing not in bulk feed).

**Notice tagging**:
- `notice_type` = `"tax_delinquent"` (Madison) or `"tax_sale"` (Jefferson — annual auction roster)
- `notice_subtype` = `"tier_distress_proxy"` (occupied) or `"tier_distress_proxy_absentee"` (Jefferson absentee subset)
- Same `tax_high_exposure` / `individual_owner` / `tier_*` tags as the regular tax pipeline fire on top

**Jefferson year-walkback**: Jefferson publishes one tax-sale roster per year before the May auction. `current_al_tax_year()` may point to a year not yet published; the proxy walks back up to 3 years to find data.

**Madison feed seasonality**: Madison's `DelinquentParcels` page resets after the annual May auction (auction was 2026-05-06; the feed was empty as of 2026-05-12 because all delinquent parcels just sold or redeemed). The pipeline will return Madison results once new delinquencies accumulate over the next 6-12 months.

CLI:
```bash
# Both counties, in-tier only, $5K+ balance (recommended default)
python src/distress_proxy_pipeline.py

# Jefferson absentee-owner subset only — strongest signal class
python src/distress_proxy_pipeline.py --counties Jefferson --absentee-only

# Full daily-ops mode with CSV + skip-trace + Slack
python src/distress_proxy_pipeline.py --datasift-csv --skip-trace --notify-slack
```

**Live results (2026-05-12 sample)**: Jefferson returned 84 in-tier records from 310 high-exposure individuals (27%). 57 absentee (68% of in-tier) — strongest signal. ZIP distribution: 35226 Vestavia (T1, 20), 35023 Hueytown (T1, 20), 35216 South Birmingham (T2, 17), 35244 South Hoover (T2, 8). Sample standout: MELTON TAMMY 1413 Old Rock Creek Rd Bessemer 35023 — **$95,844 owed on $33K property** (3x property value). Multiple absentee owners on $1M+ Vestavia/Hoover homes with $15-23K balances. Madison returned 0 (post-auction feed reset).

**Why this exists**: Until the Huntsville FOIA lands (or is denied and an alternative data path is built), this gives us a defensible Madison-equivalent distress signal using only data we already have. Tax-delinquent + absentee + tier ZIP is ~75% predictive of code-violation status based on industry research, so the proxy is not just busywork — these ARE actionable leads even without confirming a code violation directly.

## Alabama Code-Violation Pipeline (Jefferson + Madison)

Code-enforcement data is shaped completely differently from tax / probate / foreclosure — there is no symmetric two-county adapter pattern because the cities expose code violations in completely different ways:

| County / city | Primary source | Format | Coverage |
|---|---|---|---|
| **Huntsville (Madison)** | Monthly **Unsafe Building List** PDF on huntsvilleal.gov | 6-page PDF, 3-column layout (Case Created / Case Number / Address) | Highest-distress signal — every record is a property the city has formally declared uninhabitable. ~220 active cases at a time. |
| **Birmingham (Jefferson)** | 311 Portal (complaint-only, **not searchable**) + condemnation hearings on alabamapublicnotices.com | APN newspaper notices | Reuses existing APN scraper with `CONDEMNATION` / `DEMOLITION` / `PUBLIC NUISANCE` keywords (TODO — Phase 2) |

### Huntsville adapter — [src/huntsville_unsafe_buildings_api.py](src/huntsville_unsafe_buildings_api.py)

The City of Huntsville switched from a real-time HTML page (`apps.huntsvilleal.gov/unsafe/...`) to a monthly-published PDF at `/wp-content/uploads/{YYYY}/{MM}/{MM}-{YYYY}-Unsafe-Building-List.pdf` (e.g. `04-2026-Unsafe-Building-List.pdf` is the April 2026 snapshot).

The adapter auto-discovers the most recent published list by walking back from the current month (up to 6 months) until it finds one. pdfminer extracts the 3-column layout, and a per-line regex pairs date+case lines with address lines in document order.

**One technical wrinkle**: huntsvilleal.gov is behind a WAF that fingerprints httpx and rejects it with `403 Forbidden`. The adapter uses the `requests` library instead (urllib3 under the hood doesn't trigger the same fingerprint detection).

**Each `HuntsvilleUnsafeRecord`** exposes:
- `case_number` (e.g. `CE-24-5123`)
- `case_created` — case-opening date (YYYY-MM-DD)
- `case_age_years` — derived; the highest-distress cases are multi-year
- `address` / `city` / `state` / `zip` — parsed from `<street>, Huntsville, AL <zip>`
- `address_full` — raw string as printed in the PDF (preserves unit notation)
- `list_published` — date stamp of the source PDF

**Filter parameters** on `fetch_unsafe_buildings(year, month, min_age_years)`:
- `year` / `month` — explicit override; otherwise auto-discovery
- `min_age_years` — drop newer cases. Most distressed properties have been on the list for 2+ years (the city has tried and failed to get the owner to comply). Recommended: `min_age_years=2` for the highest-conversion subset.

CLI:
```bash
python src/huntsville_unsafe_buildings_api.py                          # latest list (~222 records)
python src/huntsville_unsafe_buildings_api.py --year 2026 --month 4    # specific month
python src/huntsville_unsafe_buildings_api.py --min-age-years 2        # 2+ year-old cases only
```

### NoticeData conversion

`to_notice_data(rec)` sets:
- `notice_type = "code_violation"` (auto-mapped to `Lists = "Code Violation"` in DataSift)
- `notice_subtype = "unsafe_building"` — the action signal. Every parcel on this list has been formally declared uninhabitable, so the formatter fires both the `unsafe_building` tag (descriptive category) and the `demolish` tag (actionable: this is a tear-down, not a rehab). Filter presets can route these into a different sequence — outreach scripts should frame the conversation around tear-down economics (lot value, demo cost, build-back ARV) rather than fix-and-flip.
- `case_number` — surfaces in DataSift's `Probate Case Number` column (column name is misleading but functional; case # is the unique identifier across both probate and code-violation records)
- `municipality = "Huntsville"` — fires `municipality_huntsville` tag for filter presets
- `address` / `city` / `zip` — parsed from the situs string
- `date_added` = list-published date; `raw_text` includes the case-opened date and case age

#### DataSift filter-preset compositions

```
Huntsville tear-down candidates (every record qualifies):
  notice_type:code_violation AND madison AND demolish

10+ year chronic-distress demolish targets:
  notice_type:code_violation AND demolish AND <case-age filter via Notes/case_number>

Tear-downs WITH owner enrichment (run --enrich-owners first):
  notice_type:code_violation AND demolish AND owner_name:!""
```

**Important — owner is unknown.** The Huntsville PDF doesn't include owner names. To enrich with the owner of record, pair this output with a follow-up Madison property API call by address (the Madison property search supports address-search; we just haven't wired the address-mode call yet). For Phase 1 the records flow without owner; the `owner_name` slot stays blank and DataSift sequences will need address-only outreach (postcards, door-knocking) until owner enrichment lands.

### Live verification — April 2026 snapshot

```
Huntsville unsafe-building cases: 222 (parsed from 226 raw records; 4 lost to multi-line address blocks)
By ZIP: 35810=63, 35805=49, 35811=33, 35816=28, 35801=20, 35802=15, 35803=7, 35806=7
By age: <1yr=94, 1-2yr=78, 3-5yr=26, 6-10yr=16, 10+yr=8

Oldest case: 3042 Boswell Dr Nw, opened 2004-07-29 — 21 years on the unsafe list.
```

The 10+ year cases are the most distress-saturated; many have been through multiple owners while the city has tried unsuccessfully to compel demolition or repair.

### Phase 3 — Owner enrichment via Madison address-search

The Huntsville Unsafe Buildings PDF contains no owner names. Phase 3 adds an `enrich_with_owner(notice)` helper that fills `owner_name`, `tax_owner_name`, and `parcel_id` by piping the situs address back through the Madison property API in **address-search mode** (a separate AssuranceWeb form mode we didn't expose in earlier work).

#### `madison_property_api.search_by_situs_address(street_number, street_name)`

AssuranceWeb's address mode takes the street **number** and **street name root** as two separate criteria fields:
- `Criteria1 = "3042"` (the house number)
- `Criteria2 = "Boswell"` (root only — suffixes/directionals stripped)

The adapter handles three normalization layers automatically:
1. **Suffix + directional stripping** — `"Boswell Dr Nw"` → `"Boswell"` via `_STREET_TRAILER_RE` (a list of standard USPS suffixes + N/S/E/W directionals).
2. **Unit/parenthetical stripping** — `"Cerro Vista St Sw Unit D #Unit D"` → `"Cerro Vista"` so multi-unit notation doesn't poison the query.
3. **Spelled-ordinal → digit form** — `"Tenth Ave Sw"` → `"10th"` because the assessor stores numbered streets in digit form (`"10TH AVE"`, never `"TENTH AVE"`). The mapping covers First through Twentieth.

Example:
```python
from madison_property_api import search_by_situs_address
matches = search_by_situs_address("3042", "Boswell Dr Nw")
# → [MadisonPropertyRecord(owner_name="JONES, COUNCIL", parcel_number="14-06-24-2-002-016.000", ...)]
```

#### Wiring into the Huntsville adapter

`huntsville_unsafe_buildings_api.to_notice_data(rec, enrich_owner=True)` opt-in flag — defaults False so unenriched bulk pulls stay free. CLI:

```bash
python src/huntsville_unsafe_buildings_api.py --min-age-years 2 --enrich-owners
```

#### Empirical hit rate

Across the April 2026 unsafe-building list (222 active cases), the enrichment finds an owner for ~80% of records on a 30-record sample. The remaining ~20% are real-world data limits the adapter can't paper over:

| Miss type | Cause |
|---|---|
| `3302 Cerro Vista St Sw Unit A/B/D` | Multi-unit condemned condo — assessor indexes each unit under a different parcel format that isn't a standard "number + street" lookup |
| `1308 Boxwood Dr Nw (unit A-D)` | Same — a multi-unit demolition order |
| `1008 Mckinley Ave Ne` | Property genuinely not on the current Madison tax roll (likely tax-exempt or already demolished and de-listed) |

Recommended posture: run owner enrichment opt-in (it's ~1 HTTP call per record, ~3-4 minutes for the full 222), accept the ~20% gap, and treat the missing-owner records as address-only outreach (postcards / door-knocking).

### Phase 2 — APN code-violation scraper

Six new code-violation entries in `SAVED_SEARCHES` (3 per county) reuse the existing APN scraper to pick up condemnation hearings and demolition orders that get published in newspapers. Every search uses `notice_subtype="unsafe_building"` so the DataSift formatter automatically fires the `demolish` tag — same posture as Phase 1.

#### Keyword tightening (lessons from live recon)

Naive keywords had severe false-positive rates:
| Keyword | False-positive rate | Why |
|---|---|---|
| `CONDEMNATION` alone | 100% (4/4 in recon) | Catches drug/firearm forfeitures + ALDOT eminent-domain |
| `DEMOLITION` alone | 100% (5/5 in recon) | Catches construction bid solicitations |
| `PUBLIC NUISANCE` alone | ~40% | Mixed; catches overgrown-grass complaints alongside real teardowns |

The fix is to AND-combine action verbs with the legal-template phrasing AL § 11-53A-20 mandates for actual teardown publications:

| Search | What it catches |
|---|---|
| `DEMOLITION UNSAFE STRUCTURE` (AND) | Resolution-ordering-demolition format (AL § 11-53A-20 boilerplate) |
| `CONDEMNED STRUCTURE DEMOLITION` (AND) | "ordered the demolition of the condemned structure located at..." |
| `NUISANCE ABATEMENT DEMOLISHED` (AND) | City-council nuisance-abatement orders ("declared the structure ... a public nuisance and order it demolished/secured") |

Combined exclude_terms = `bid contractor sealed` to drop construction-bid leakage.

#### Coverage reality

In this 14-day window, **all real teardown publications were from Tuscaloosa / Mobile / Albertville** — zero Jefferson or Madison hits. This confirms what we suspected during the original research:

- **Birmingham** primarily uses its 311 portal (no public read) for code enforcement; condemnation hearings rarely make it to APN
- **Huntsville** publishes its own monthly Unsafe Building List PDF (already covered by Phase 1)
- **Smaller cities** (Bessemer, Hoover, etc.) similarly use city-website + posted physical notices

So the canonical Phase 2 yield will be sparse — maybe 1-5 Jefferson/Madison teardown notices per quarter. But when they do publish, the filter is clean and the records flow into the same `demolish`-tagged pipeline as the Huntsville list. `is_target_county()` correctly drops the cross-county notices that the keyword search picks up.

#### `SearchConfig.notice_subtype` field

New optional field on `SearchConfig` — when set, the scraper writes it through to `notice.notice_subtype` for every record from that search. Used today exclusively for code-violation searches but available for any future search-driven subtype classification. Both `_notice_from_snippet` (snippet path) and `_scrape_notice` (post-CAPTCHA path) honor it; `parse_notice_page`'s auto-detection (probate subtypes) takes precedence when both fire.

### Phase 4 — Birmingham Accela early-distress scraper

Birmingham's Accela Citizen Access portal exposes a public code-enforcement search at [aca-prod.accela.com/BIRMINGHAM](https://aca-prod.accela.com/BIRMINGHAM/Cap/CapHome.aspx?module=Enforcement) — earlier research dismissed it as permit-only, but it actually has six enforcement record-types covering the full distress funnel:

| Accela Record Type | CLI key | NoticeData subtype | Tags fired |
|---|---|---|---|
| Condemnation | `condemnation` | `unsafe_building` | `unsafe_building, demolish` (matches Phase 1 posture) |
| Housing Property Maintenance | `housing` | `housing_enforcement` | `housing_enforcement, early_distress` |
| Inoperable Vehicles | `vehicles` | `inoperable_vehicle` | `inoperable_vehicle, early_distress` |
| Environmental Enforcement | `environmental` | `environmental_enforcement` | `environmental_enforcement, early_distress` |
| Zoning Enforcement | `zoning` | `zoning_enforcement` | `zoning_enforcement, early_distress` |
| Environmental Batch Record | not surfaced | (skipped) | bulk env. enforcement; low individual-record value |

The `early_distress` tag is the new bottom-of-funnel signal: owner is still in the property but slipping on maintenance. Reach-out window is months-to-years before the property hits foreclosure or unsafe-building lists. Outreach framing is "rehab/clean-up offer", **not** the tear-down framing the `demolish` tag uses for unsafe-building records.

#### Adapter — [src/birmingham_code_enforcement_api.py](src/birmingham_code_enforcement_api.py)

Playwright-based (Accela is ASP.NET WebForms with `__VIEWSTATE` postbacks; not driveable via plain `requests` or `httpx`). Per-category flow:
1. GET the search form
2. Fill date range + select Record Type
3. POST search via `__doPostBack` (Playwright handles the postback)
4. Extract data rows from the `gdvPermitList` table — rows with class `ACA_TabRow_Odd` / `ACA_TabRow_Even`. Cell layout: `[checkbox, date, address, case#, type, description, status, _, address_dup]`.
5. Click "Next >" pagination link until `max_pages` reached or no more pages

Each `BirminghamEnforcementRecord` exposes `case_number` (e.g. `HEN2026-00330`), `case_opened`, `address`, `category`, `notice_subtype`, `description`, `status`, plus four **detail-page-only** fields (`owner_name`, `owner_address`, `fee_total`, `fee_balance`) populated when `enrich_details=True`.

CLI:
```bash
# Default: all 5 distress categories, last 30 days, max 5 pages each (~250 cases)
python src/birmingham_code_enforcement_api.py

# Specific categories + longer window
python src/birmingham_code_enforcement_api.py --category housing,vehicles --days 60 --max-pages 10

# Detail-page enrichment via Accela (slow — ~3s per record extra; pulls fees, mailing address, deceased flags)
python src/birmingham_code_enforcement_api.py --enrich-details --max-pages 2

# Owner enrichment via Jefferson tax-roll API (fast — ~0.3s per record; address-search lookup)
python src/birmingham_code_enforcement_api.py --enrich-owner --max-pages 2

# Both layers (Jefferson tax-roll first, then Accela detail-page only when owner still missing)
python src/birmingham_code_enforcement_api.py --enrich-owner --enrich-details --max-pages 2
```

#### Volume reality

In a 30-day window, Housing Enforcement alone returns ~100+ cases; Inoperable Vehicles also ~100+. Across all 5 categories, expect ~300-500 new Birmingham distress cases per month — a substantial new lead source.

#### Detail-page enrichment

The list view doesn't include owner name or fine amount. Opt-in `--enrich-details` flag clicks each case to extract:
- **Owner name + mailing address** (often deceased-flagged, e.g. `"ORR LAWANDA M AGT OF HEIR FOR ORR PAMELA *"` — cross-source probate/code-violation hit)
- **Total fees assessed** (when present)
- **Fee balance** (still owed)

Fees are mapped to the existing `tax_delinquent_amount` slot since DataSift's standard 80-column schema doesn't have a separate "code-violation fees" column — column-name "Tax Deliquent Value" is misleading but the same `tax_high_exposure` filter-preset tags fire when fees ≥ $5K, so high-exposure code-violation balances surface alongside high-exposure tax delinquencies.

#### Owner enrichment via Jefferson property API

Birmingham code-enforcement records flow through `jefferson_property_api.search_by_situs_address()` (added in this phase — Jefferson E-Ring `searchtype=4` was previously unexposed; mirrors the Madison adapter's address-search). The address parser `_parse_birmingham_address()` splits the Accela "STREET, CITY ST ZIP" string before search, and `normalize_jefferson_city()` collapses BHAM → Birmingham, MOUNTAIN BRK → Mountain Brook, etc., so DataSift's Property City column stays clean.

`enrich_with_owner(notice)` is the public entry point on the Birmingham adapter. CLI opt-in via `--enrich-owner`. Empirical hit rate ~80% in live testing (10/10 in a recent housing+vehicles batch); the gap is mostly atypical street-name formats. Directional fallback (`_LEADING_DIRECTIONAL_RE`) strips a leading compass token between the house number and street name on retry — fixes cases like `1124 SW 16TH ST SW` and `120 N 68TH PL N` that the assessor index files without the leading directional.

This is faster than `--enrich-details` (Jefferson API ~0.3s vs Accela detail-page ~3s) and free (vs Playwright session cost). Pair both flags when fees/deceased-flags are also wanted: `--enrich-owner` fills owner first, `--enrich-details` then adds fees + mailing address + Accela's deceased annotations on top.

#### DataSift filter-preset compositions

```
Tear-down candidates (cross-source — Phase 1 + Phase 2 + Phase 4 condemnation):
  notice_type:code_violation AND demolish

Birmingham early-distress (housing + vehicles + environmental + zoning):
  notice_type:code_violation AND birmingham AND early_distress

Birmingham junk-vehicle leads only:
  notice_type:code_violation AND birmingham AND inoperable_vehicle

Birmingham IPMC housing violations only:
  notice_type:code_violation AND birmingham AND housing_enforcement

Birmingham high-fee code violations (after enrich_details):
  notice_type:code_violation AND birmingham AND tax_high_exposure
```

### Phase 6 — Hoover SeeClickFix code-enforcement adapter

[src/hoover_code_enforcement_api.py](src/hoover_code_enforcement_api.py) — Hoover (Jefferson County, Tier 1 ZIP 35022 + Tier 2 35244 + adjacent 35226 / 35216) routes citizen-reported code violations through SeeClickFix's 311-style platform. The web portal is at `https://seeclickfix.com/web_portal/cfK8xFcB5G2XrMX9VzD1cLSc` and the public API is at `https://seeclickfix.com/api/v2/issues` (no auth, browser-like User-Agent + Referer required to bypass anti-bot).

**Filtering approach** — the SeeClickFix `web_portal_id` URL parameter is silently ignored by the API; instead we filter by **lat/lng + zoom** (33.4054, -86.8114, zoom=11 covers ~10mi radius bounding Birmingham metro), then post-filter strictly on `"hoover"` in the address string. ~360 strictly-Hoover issues at any time across all categories; ~30 are dedicated `CODE ENFORCEMENT` per 30 days = ~1/day.

**Signal class** — these are **citizen-reported complaints** (early-stage distress: overgrown grass, junk vehicles, dilapidated property), not formal code citations. Softer evidence than Huntsville's Unsafe Buildings list but earlier in the funnel — most issues are still Open or Acknowledged at time of pull. Sets `notice_subtype="code_enforcement_complaint"` to distinguish from `unsafe_building` (Huntsville) and `housing_enforcement` / `inoperable_vehicle` / etc. (Birmingham Accela subtypes).

**Address parsing** — anchor-based: locate the literal `"Hoover"` in the address string, treat everything before it as the street, capture the trailing 5-digit ZIP. Tolerates the three formats SeeClickFix users enter (`"... Hoover, Alabama, 35244"`, `"... Hoover, AL, 35226, USA"`, `"... Hoover AL 35226, United States"`).

**Owner enrichment via Jefferson E-Ring** — `enrich_with_owner()` mirrors the Birmingham Accela adapter: passes `notice.address` to `jefferson_property_api.search_by_situs_address()`, picks exact-situs match if available. ~80% hit rate. Adds owner_name, parcel_id, assessed_value, is_homestead.

CLI:
```bash
# Default: last 30d, target ZIPs only, no owner enrichment
python src/hoover_code_enforcement_api.py

# Full daily-ops mode with owner enrichment + open-only + CSV
python src/hoover_code_enforcement_api.py --days-back 7 --target-zips-only \
    --enrich-owners --open-only --csv-out output/hoover_codes.csv

# Distribution recon (no request_type filter — useful to characterize Hoover SeeClickFix volume)
python src/hoover_code_enforcement_api.py --all-types --max-pages 5
```

**Realistic 30-day yield (May 2026 sample)**: 30 in-tier issues across 35226 (T1, 19), 35216 (T2, 6), 35244 (T2, 5). Status mix: 15 Open + 4 Acknowledged + 11 Closed. Owner enrichment resolved cleanly on all sampled records (real homeowner names + parcels + valuations $286K-$529K).

### Status

- **Phase 1 — Huntsville unsafe buildings (DONE)**: 222 active cases per monthly PDF.
- **Phase 2 — APN code-violation scraper (DONE)**: 6 saved searches with tightened keywords + `notice_subtype="unsafe_building"`. Coverage is intentionally narrow (low Jefferson/Madison APN volume) but every hit lands in the `demolish`-tagged sequence cleanly.
- **Phase 3 — Owner enrichment via Madison address-search (DONE)**: `search_by_situs_address()` + `enrich_with_owner()`. ~80% hit rate on Huntsville unsafe-building records.
- **Phase 4 — Birmingham Accela early-distress scraper (DONE)**: Playwright-based adapter for 5 enforcement categories. Adds ~300-500 Birmingham early-distress records per month with `early_distress` tag (separate from `demolish`). Optional detail-page enrichment for owner + fees.
- **Phase 5 — Birmingham owner enrichment via Jefferson tax-roll (DONE)**: `jefferson_property_api.search_by_situs_address()` (E-Ring `searchtype=4`) + `birmingham_code_enforcement_api.enrich_with_owner()`. CLI flag `--enrich-owner`. ~80% hit rate; directional-fallback retry handles `1124 SW 16TH ST SW`-style addresses. ~10x faster than `--enrich-details` and works without an Accela session.
- **Phase 6 — Hoover SeeClickFix code-enforcement (DONE, 2026-05-08)**: Citizen-complaint adapter for Hoover (Tier 1 + Tier 2 ZIPs). ~30 in-tier issues per 30 days. `notice_subtype="code_enforcement_complaint"`. Wired into `code_violation_pipeline._fetch_hoover()` with `include_hoover=True` default; toggle via `--no-hoover`.

### Other Birmingham-metro municipalities — researched 2026-05-08, NOT viable for online scraping

Researched these 4 cities at the user's request — each was confirmed to have no publicly-scrapeable code-enforcement data. Decision: stay APN-only for these cities; do not re-research without new evidence.

| City | Tier ZIPs | Platform | Why not viable |
|---|---|---|---|
| **Vestavia Hills** | T1 35226 | Vestavia Hills Connect + OpenGov (permits only) | Connect is anonymous-submit-only with no public list. OpenGov record types 1071/1082 cover permits (Portable Storage, Land Disturbance), not violations. |
| **Trussville** | T2 35173 | `compliance.trussville.org` — **Freshdesk** ticket portal | Public can SUBMIT tickets; viewing requires staff login. Freshdesk API is paid + auth-gated. |
| **Hueytown** | T1 35023 | `hueytown.govtportal.com` | Payments only (citations/utility bills/business licenses). No code-violation search. |
| **Pinson** | T2 35126 | `pinsonalabama.com` returns ~114 bytes | Essentially no municipal web presence — small population (~7K), no public infrastructure. |

**SeeClickFix probe**: Each city was also probed against the SeeClickFix API (same source as Hoover). Volume was 0-7 issues per city across 2 pages of bbox queries — essentially absent. Generalizing the Hoover SeeClickFix adapter to these cities yields no usable signal.

**Floor coverage**: The APN code-violation scraper (Phase 2) catches FORMAL condemnation/demolition publications statewide via tightened keywords (`DEMOLITION + UNSAFE STRUCTURE`, `CONDEMNED STRUCTURE`, `NUISANCE ABATEMENT DEMOLISHED`). When these cities publish formal teardowns, they're already in the daily scrape. Early-distress signals (housing maintenance, junk vehicles, zoning) are not available for these 4 cities — they live in private/paid platforms.

**Path to close the gap (if ever needed)**: File recurring open-records requests to each city's code-enforcement office (parallel to the planned Huntsville FOIA at [docs/foia/huntsville_code_enforcement_request.md](docs/foia/huntsville_code_enforcement_request.md)). Manual workflow, monthly cadence, requires human-in-loop to file/receive/import. Worth the effort only if APN volume proves insufficient.

### Coverage gap (planned to close via FOIA)

Huntsville's softer code violations (tall grass / inoperable vehicles / IPMC / zoning) are NOT covered by the public scrape. Huntsville handles these through the Huntsville Connect portal (submit-only, no public search) — only the formal Unsafe Building list (Phase 1) is publicly readable.

Closing this gap requires a recurring Alabama Open Records Act request to Huntsville's Community Development / Code Enforcement Division. A copy-paste-ready letter is at [docs/foia/huntsville_code_enforcement_request.md](docs/foia/huntsville_code_enforcement_request.md) — it asks for the same field set the Birmingham Accela adapter already pulls (case#, address, owner, fees, balance, status) on a monthly recurring schedule, mapped to the existing `NoticeData` schema. Once the export is flowing, build a `huntsville_code_violations_api.py` adapter parallel to `birmingham_code_enforcement_api.py` and wire it into `_fetch_madison()` in `code_violation_pipeline.py`.

### Unified pipeline — [src/code_violation_pipeline.py](src/code_violation_pipeline.py)

Single orchestrator that runs all city adapters in one pass and converts to `NoticeData`. Mirrors the `tax_distress_pipeline.py` shape:
- `fetch_code_violations(counties=("Madison","Jefferson"), ...)` — public API. Returns combined `NoticeData` list across all selected cities.
- Per-city wrappers: `_fetch_madison()` (Huntsville Unsafe Buildings PDF), `_fetch_jefferson()` (Birmingham Accela), `_fetch_hoover()` (Hoover SeeClickFix — fires when Jefferson is selected AND `include_hoover=True`).
- Knobs are passed through: Birmingham gets `categories`, `days_back`, `max_pages`, `enrich_details`, `headless`; Huntsville gets `min_age_years`; Hoover gets `include_hoover` (default True) and `hoover_target_zips_only` (default True). All three share `enrich_owner` (tax-roll address-search — Madison API for Huntsville, Jefferson E-Ring for Birmingham + Hoover).

CLI examples:
```bash
# Both counties, default windows (most recent Huntsville PDF + Birmingham last 30 days)
python src/code_violation_pipeline.py

# Phase 1 high-conversion subset only (Huntsville cases ≥ 2yrs + Birmingham condemnation)
python src/code_violation_pipeline.py --min-age-years 2 --categories condemnation

# Full feed with owner enrichment + DataSift CSV
python src/code_violation_pipeline.py --enrich-owner \
    --output-datasift-csv output/code_violations_datasift.csv

# Birmingham early-distress only, 60-day window, 10 pages per category
python src/code_violation_pipeline.py --counties Jefferson --days 60 --max-pages 10
```

## Marshall County, AL — Distressor Coverage (added 2026-05-12)

Marshall County (Albertville-anchored, ~99K population, north-central AL) was added as the third active county after Jefferson + Madison. The build economy was large because Marshall shares Madison's vendor platform: **AssuranceWeb (countygovservices.com)** for both property search and tax-delinquent recording. The Madison property + probate adapter patterns clone almost directly.

### Tier ZIPs

Codified in [src/target_zips.py](src/target_zips.py) as `MARSHALL_TIER_1` (6 ZIPs) and `MARSHALL_TIER_2` (5 ZIPs). Source-of-truth analysis at `~/Documents/Claude/Projects/REI Skill Library/Marshall_County_AL_SFR_125K_500K_Market_Analysis.md`. Codification is a **union** of the MD volume rankings AND operator-strategic additions (2026-05-12):

| Tier | ZIPs | Coverage |
|---|---|---|
| **Tier 1** | 35950, 35976, 35016, 35961, 35951, 35957 | Albertville (35950 + 35951 fringe), Guntersville/Union Grove (35976), Arab (35016), Boaz (35961), Crossville/Geraldine (35957). First four are MD's volume-ranked picks; 35951 + 35957 operator-promoted for full Albertville-zone + Crossville-corridor coverage. |
| **Tier 2** | 35962, 35175, 35747, 35769, 35980 | Boaz area (35962, MD-ranked) + four border ZIPs (35175 Cullman line, 35747 Grant supplemental, 35769 Jackson line / Scottsboro, 35980 DeKalb line). Border ZIPs capture cross-county property where the owner lives over the line. |

### Data sources

| Distressor | Source | Module | Status |
|---|---|---|---|
| **Property search** | AssuranceWeb `marshall.countygovservices.com/property/Property/Search` | [src/marshall_property_api.py](src/marshall_property_api.py) | **Live**. Clone of `madison_property_api`. Live-verified 936 SMITH hits, 14 SMITH JOHN hits. |
| **Probate (post-probate)** | APN newspaper publications + `_search_marshall` enrichment | Reuses [src/apn_probate_pipeline_al.py](src/apn_probate_pipeline_al.py) + [src/probate_property_locator.py](src/probate_property_locator.py) | **Live**. `_TARGET_COUNTIES` updated; `_adapter_for("Marshall")` routes to `_search_marshall`. |
| **Pre-probate** | legacy.com Marshall County aggregator (`/us/obituaries/local/alabama/marshall-county`) | Reuses [src/pre_probate_pipeline_al.py](src/pre_probate_pipeline_al.py) | **Live**. `ALABAMA_LISTINGS["Marshall"]` added; `LEGACY_MARKET_URL_RE` broadened to accept any market slug (e.g. `albertville`, `guntersville`, `marshall-county`). |
| **Foreclosure** | APN newspaper publications | 2 SAVED_SEARCHES (MORTGAGE FORECLOSURE SALE / RESCHEDULE) | **Live**. |
| **Code-violation** | APN-floor only (no online municipal source) | 3 SAVED_SEARCHES (DEMOLITION+UNSAFE, CONDEMNED+DEMOLITION, NUISANCE ABATEMENT DEMOLISHED) | **Live**. `code_violation_pipeline.py` accepts `--counties Marshall` but no-ops with a log line — the APN scraper carries Marshall's code-violation coverage. |
| **Tax-delinquent** | AssuranceWeb `marshall.countygovservices.com/property/Property/DelinquentParcels` | [src/marshall_tax_delinquent_api.py](src/marshall_tax_delinquent_api.py) | **STUB — source disabled by the county** ("The Delinquent Parcels listing is currently disabled" + "Payments are currently disabled" as of 2026-05-12). Adapter probes the page on each call via `is_source_disabled()` and returns `[]` while offline. The orchestrators (`tax_distress_pipeline.py`, `distress_proxy_pipeline.py`) wire the stub through; the moment Marshall re-enables the listing, back-fill the parser in `fetch_delinquent_parcels()` (`NotImplementedError` is raised today if the page comes back online, by design — to prevent silent data loss). |

### Why no dedicated code-enforcement adapter

Researched 2026-05-12: Marshall has no Accela / SeeClickFix / municipal-portal exposure for code violations. Albertville, Boaz, Guntersville, and Arab all handle code enforcement through internal staff workflows with no public read interface. SeeClickFix probe across all four cities returned essentially zero issues — citizen-complaint volume is too low to justify a dedicated adapter. **Coverage path**: the APN-floor scraper (3 Marshall SAVED_SEARCHES on tightened keywords) catches FORMAL condemnation/demolition publications statewide when Marshall cities file them. Same posture as the Birmingham-metro dead-end cities (Vestavia/Trussville/Hueytown/Pinson).

### Smarty ZIP recovery for Marshall

Marshall property records (like Madison) return only street in `search_by_owner_name` — city/zip are not in the bulk response. `_smarty_zip_for_marshall_address()` (in `pre_probate_pipeline_al.py`) uses **`"Albertville AL"`** as the USPS-CASS anchor; Smarty resolves Boaz (35957), Guntersville (35976), Arab (35016), and rural Marshall addresses to their correct delivery cities from this anchor automatically. Generalized as `_smarty_zip_for_assuranceweb_address(situs, lastline_hint)` so future AL counties on the same vendor platform plug in cleanly.

### Cross-county property ownership

The pre-probate orchestrator's `search_order` rotates all 3 counties (`_search_jefferson`, `_search_madison`, `_search_marshall`) with the hinted county first. Decedent died in Huntsville but owned property in Boaz? The Madison hint loses, Marshall wins on the fallback. The `obituary_harvester` regex change (broader market-slug pattern) also lets the harvester catch obits syndicated under any city slug from the Marshall County aggregator listing.

## Key Domain Rules

- **Foreclosure filtering is critical.** Not all notices from "Foreclosure" saved searches are actual foreclosures. The scraper parses each notice's full text and only includes ones with trustee sale language. See `INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`.
- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Owner names** in foreclosure notices typically appear after "executed by" in the deed of trust language.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

## Apify Deployment

The project runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: set up daily schedule and configure secrets in Actor input
```

### Actor Input (configured in Apify Console or `input.json`)
- `mode`: "daily" or "historical"
- `counties` / `types`: arrays to filter saved searches (empty = all)
- `tn_username`, `tn_password`, `captcha_api_key`: secrets (required)
- `google_drive_folder_id`, `google_service_account_key`: optional Google Drive upload

### Actor Output
- **Dataset**: structured records pushed via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file uploaded via service account

### Key Files
- `.actor/actor.json` — Actor manifest (name, version, Dockerfile path)
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — Based on `apify/actor-python-playwright:3.12`
- `src/drive_uploader.py` — Google Drive upload via base64-encoded service account key
- `input.json` — Local test input (gitignored, contains credentials)

## Courthouse Photo Pipeline (build 1.0.28+)

Courthouse terminal photos → OCR → LLM parse → enrichment → DataSift. Runner takes phone photos at Knox/Blount county terminals, uploads to Dropbox organized as `{county}/{notice_type}/`, system auto-processes.

### Notice Types (7 total)
- `foreclosure`, `tax_sale`, `tax_delinquent`, `probate` — existing from web scraper
- `eviction` — plaintiff = landlord (target contact), defendant = tenant
- `code_violation` — owner of record, violation type, compliance deadline
- `divorce` — petitioner + respondent, property from schedule page

### Critical OCR Patterns (hard-won from live testing)

**Moire pattern from terminal screens is the #1 OCR killer.** Standard Tesseract preprocessing (adaptive threshold, CLAHE) produces garbage on courthouse terminal photos. The fix:
- **Bilateral filter** (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges
- **Otsu threshold** (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after bilateral — auto-determines optimal binary threshold
- **PSM 4** (single column variable text) for terminal screens — NOT PSM 6 (single uniform block) which was the research recommendation but fails in practice
- **Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos** — EXIF transpose handles rotation. OSD on raw phone images often fails and the 270° fallback rotates correct images sideways

### Probate Deep Prospecting (from courthouse terminals)

Courthouse probate records have decedent name + PR/executor name but NO property address. Multi-tier lookup fills the gap:

**Property Address Lookup** (Step 3c in enrichment pipeline):
1. **Tier 1: Knox Tax API name search** — search `/parcels/{decedent_name}`, score by token overlap (FIRST MIDDLE LAST → LAST FIRST MIDDLE), accept >= 0.4 match. Tries multiple name variations (with/without suffix, LAST FIRST format, first+last only).
2. **Tier 2: Executor family search** — search Knox Tax API by executor name, look for properties where decedent's last name appears in owner field (family property transferred to executor).
3. **Tier 3: People search** — search TruePeopleSearch/FastPeopleSearch for decedent's last known Knox County address.

**Probate Preset** (obituary enricher):
- Triggers when court record has PR name + decedent name (no address required) — prevents wrong obituary from overriding court-named executor
- Sets DM = the named PR/executor directly, skips obituary search entirely
- Then runs DM address lookup (Knox Tax API → People Search → Tracerfy)

**DOD Sanity Check** (obituary enricher):
- Rejects obituary matches where DOD is > 3 years before the notice filing date (`MAX_DOD_GAP_YEARS = 3`)
- Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name)
- Applied to both full-page and snippet matches

### Dropbox Folder Structure
```
{DROPBOX_ROOT_FOLDER}/
├── Knox/
│   ├── eviction/
│   ├── code_violation/
│   ├── divorce/
│   ├── foreclosure/
│   ├── tax_sale/
│   └── probate/
└── Blount/
    └── (same subfolders)
```

### Environment Variables
- `DROPBOX_APP_KEY` — Dropbox OAuth2 app key
- `DROPBOX_APP_SECRET` — Dropbox OAuth2 app secret
- `DROPBOX_REFRESH_TOKEN` — Dropbox offline refresh token (auto-rotates access tokens)
- `DROPBOX_POLL_INTERVAL` — seconds between polls (default 900 = 15 min)
- `DROPBOX_ROOT_FOLDER` — root folder path in Dropbox (e.g., "TN Public Notice")

### Dependencies (added to requirements.txt)
- `opencv-python-headless>=4.13.0` — image preprocessing (headless = no GUI, saves 26MB in Docker)
- `numpy>=1.26.0` — required by OpenCV
- `dropbox>=12.0.2` — Dropbox SDK (minimum for post-Jan-2026 API compatibility)

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns. There is **no REST API** — upload is via Playwright browser automation of the web UI.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (41 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (80 columns as of 2026-04)
- **Core auto-mapped (10):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP
- **Phone/Email + meta (17):** Phone 1–9 (Tracerfy), Email 1–5, Tags, Lists, Notes
- **Built-in fields (18):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Deliquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields — SiftStack group (15):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL
- **Deep prospecting fields (10):** DM 1/2/3 Status, DM 1 Source, Heir Count, Heirs Living, Signing Chain Count/Names, DM Confidence Reason, Data Flags
- **Entity research fields (3):** Entity Type, Entity Contact, Entity Contact Role
- **AL probate enrichment fields (7, added 2026-04):** Probate Case Number, Judge of Probate, Probate Subtype (`probate_creditors`/`probate_sale`/`probate_heirs_notice`), Petition Filed Date, Hearing Date, Creditor Claim Deadline, Total Estate Value

### Built-in field mapping nuances (probate)
- **Probate Open Date** — prefers `granted_date` (when Letters Testamentary were issued, per AL § 43-2-61) over `date_added` (publication date). Falls back to publication date only when granted_date wasn't extracted.
- **Personal Representative** — for probate notices, uses `owner_name` (the verified PR named in the Letters) before `decision_maker_name` (which may be an obituary-derived heir).
- **Estimated Value** — falls back to `assessed_value` (county assessor's last-assessed value) when Zillow's `estimated_value` hasn't been populated by downstream enrichment.
- **Structure Type** — falls back to `property_use` (assessor classification: Residential / Commercial / Real Property / etc.) when Zillow's `property_type` hasn't been populated.

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23). A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" tag:** Every record gets this tag — signals first-to-market county data (prioritized over bulk data in filter presets)
- **Lists column:** Maps `notice_type` → DataSift list name (`foreclosure` → "Foreclosure", `probate` → "Probate", `pre_probate` → "Pre-Probate", `tax_sale` → "Tax Delinquent", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Violation", `divorce` → "Divorce"). The Heirs CSV uses the SAME `NOTICE_TYPE_TO_LIST` mapping — heir rows land in the same distressor list as the DM row from the same source notice. To distinguish heir rows from DM rows after upload, each heir row carries a per-row `heir_of_<notice_type>` tag (e.g. `heir_of_foreclosure`). DataSift filter presets can target these with `foreclosure AND heir_of_foreclosure` to isolate the heirs-only audience inside the broader Foreclosure list.

  **Important:** the per-row `Lists` column does NOT auto-route records — the upload wizard creates ONE target list per session. To land records in the correct per-distressor list, the canonical pipeline (`upload_to_datasift_per_distressor`) splits the CSV by `Notice Type` and uploads each subset using `existing_list=True` with the mapped list name. Each target list must already exist in DataSift. Tax-sale records are intentionally consolidated under "Tax Delinquent" — the auction-roster distinction is preserved on the record via `notice_type=tax_sale` tag. **Run-summary display:** `HEIRS_DISPLAY_LABELS` (in `datasift_formatter.py`) provides "Heirs of Foreclosure" / "Heirs of Probate" / etc. labels so Slack and terminal summaries can call out heir-CSV uploads distinctly even though both DM and heir rows go to the same DataSift list.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records). **AL probate adds:** `municipality_birmingham` / `municipality_trussville` / `municipality_county` / etc. (lowercased Jefferson DispCode), `homestead`, `probate_sale` / `probate_creditors` / `probate_heirs_notice`, `multi_parcel`, `hearing_upcoming` (within 30 days), `creditor_window_open` (deadline still in future). These let filter presets target Birmingham metro core (`municipality_birmingham,municipality_hoover,municipality_vestavia_hills,municipality_mountain_brook,municipality_homewood,municipality_trussville`) or active probate-sale opportunities (`probate_sale,hearing_upcoming`).

### Upload Wizard (5 Steps)
1. **Setup:** Click "Upload File" sidebar → "Add Data" → dropdown selects either:
   - **"Uploading a new list not in DataSift yet"** (creates a SiftStack-named wrapper list — used by the legacy `upload_to_datasift` / `upload_datasift_split` helpers; do NOT use for new code).
   - **"Adding properties to an existing list inside DataSift"** (canonical path — picks the per-distressor list from the dropdown). The new `upload_to_datasift_per_distressor` helper uses this mode after splitting the CSV by Notice Type.
2. **Tags:** Skip through (tags are in CSV column)
3. **Upload File:** Set file on `input[type="file"]`
4. **Map Columns:** Core address fields auto-map; Tags, Lists, and enrichment columns may need manual mapping
5. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

**Update Data mode caveat:** the wizard's "Update Data" toggle (intended to merge into existing records instead of creating duplicates) has a different downstream step sequence than "Add Data" — a modal interrupts the file-input step and the existing helper times out. Until that flow is mapped, the canonical helper uses Add Data mode + existing-list selection. Records are matched on property address by DataSift when present, but duplicate creation is possible if the same row is uploaded twice without manual cleanup in between.

### Column Mapping Notes
- Only core address fields (Property Street, City, State, ZIP) reliably auto-map
- Tags, Lists, Estimated Value, and enrichment columns often stay unmapped in step 4
- Notes and MSL Status sometimes auto-map
- Custom fields (TN Public Notice group) require drag-and-drop mapping

### Contact Logic
- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After CSV upload, the pipeline automatically runs two DataSift actions via Playwright:

1. **Enrich Property Information** (Manage → Enrich Data): Adds SiftMap property data (beds, baths, Zestimate, sqft, sale history) to uploaded records. "Enrich Owners" and "Swap Owners" are OFF — protects our PR/DM contact mapping.
2. **Skip Trace** (Send To → Skip Trace): Pulls phone numbers (up to 5 per owner) + emails via unlimited plan ($97/mo). Adds auto-tag `skip_traced_YYYY-MM`.

Both run in background — tracked in Activity tab. Both are ON by default when `--upload-datasift` is set.

### CLI Flags
```bash
python src/main.py daily --upload-datasift        # upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich       # upload only, skip enrichment
python src/main.py daily --upload-datasift --no-skip-trace   # upload + enrich, skip skip trace
python src/main.py daily --notify-slack            # send run summary to Slack/Discord
```

### Environment Variables
- `DATASIFT_EMAIL` — DataSift login email
- `DATASIFT_PASSWORD` — DataSift login password
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook for run summaries

### Login Selectors (SPA quirks)
- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (not `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won patterns from build 1.0.22-1.0.23 (SiftMap, preset management, sequence builder). Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` elements — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value display; `[class*="SelectOptionContainer"]` = dropdown options
- Multiple Select dropdowns exist per panel (Lists, Tags, Property Status) — always target the **LAST visible one**
- Use `x > 450` bounds check in all JS queries to avoid matching sidebar elements (sidebar is 0-400px)
- React state updates require native setter + event dispatch, not just `.value = ...`:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})` instead
- Filter Presets section is at the BOTTOM of the filter panel — must scroll container down to reveal
- After scrollIntoView, element y-positions may be negative — don't filter by `y > 0` for the target element

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Must use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up phases
- "Add new Action +" button required for 2nd+ actions; first action uses initial drop zone
- Sidebar cards can scroll out of view when main area scrolls — scroll BOTH source and target into view before drag

**Pointer Interception (common blockers)**
- Beamer NPS survey iframe (`#npsIframeContainer`) blocks ALL pointer events globally — remove from DOM via `_dismiss_popups()`
- `RecordsFiltersstyles__RecordsFiltersSection` elements intercept clicks — use `page.evaluate()` JS click or `force=True`
- When Playwright click fails with "outside of viewport" or "intercept": switch to `page.evaluate(el => el.click())`
- SiftMap PropertyDetails panel blocks sidebar checkboxes — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- All 21 presets have Property Status "Do not include" → "Sold" (build 1.0.23)

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name handling: detect error toast "different sequence title", retry with " V2" suffix
- Actions tab: navigate via "Set the Following Actions" button or URL (`/sequences/new/actions`)
- Autocomplete inputs: after each selection, `fill("")` + Escape to dismiss dropdown before next entry
- "Sold Property Cleanup" sequence exists in Transactions folder (build 1.0.23): Trigger (Property Tags Added) → Condition (Sold) → Actions (Status→Sold, Remove Lists, Clear Tasks, Clear Assignee)

**SiftMap Automation**
- Search by city (NOT county): Knox → "Knoxville, TN", Blount → "Maryville, TN"
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters (price, date) set values visually but don't trigger React re-query. Only sidebar-visible properties (~3-5) get added per run

**Market Finder Extraction Patterns (build 1.0.29+)**

Hard-won patterns from building `extract_market_finder.py`. The Market Finder UI differs significantly from the rest of DataSift.

- **NO HTML `<table>` element** — data table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell` (styled-components). Searching for `<table>` or `<tr>/<td>` finds nothing.
- **PAGINATION, not infinite scroll** — table shows 20 rows per page with "1-20 of N" text and `PaginationInnerContainer` with prev/next `<button>` elements. Must click through ALL pages to get complete data. Knox County has 48 ZIPs (3 pages) and 120+ neighborhoods (7 pages).
- **State/County selection uses `InputMultiSearch`** — NOT styled-component Select dropdowns. Inputs have placeholders: `"Select States"`, `"Select Counties"`, `"Select ZIP Codes"`. Click input → type name → click dropdown result item (`[class*="Item"]:has-text("...")`).
- **ZIP/Neighborhood toggle is a styled Select dropdown** — at the top bar with `Selectstyles__SelectValue` showing current view. Check the displayed text BEFORE clicking — if already on the correct view, clicking toggles AWAY from it. Only click to switch if the displayed text doesn't match the desired view.
- **Beamer push modal (`#beamerPushModal`)** — appears on fresh login, blocks ALL pointer events. Different from the NPS survey (`#npsIframeContainer`). Both must be removed from DOM before any click interactions. Always call dismiss with `force=True` as fallback.
- **Page body scrolling required** — pagination controls are at `y=1867`, below the viewport (`clientH=824`). Must scroll `AdminPage__AdminPageBody` container down before pagination buttons are accessible.
- **Summary panel on right side** — shows county-level aggregates: Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month, Market Rent, Gross Rental Yield, Homeownership Rate. Extract via regex on page text.

```bash
# Extract all Market Finder data for a county
python src/extract_market_finder.py --state "Tennessee" --county "Knox" -v
python src/extract_market_finder.py --state "Tennessee" --county "Knox,Blount" --headless

# Output: JSON file in output/market_finder_{state}_{county}_{timestamp}.json
```

## REI Skill Library (13 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/` folder. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Skill Inventory

| # | File | Division | Score | What It Does |
|---|------|----------|-------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | 9.6 | Market Finder reports, zip code scoring (6 weights verified against `market_analyzer.py`), 7-sheet Excel output |
| 2 | `first-market-county-data.skill` | Market Intel | 9.7 | County clerk data extraction for all 7 notice types, FOIA templates, marketing windows |
| 3 | `buyer-prospector.skill` | Market Intel | 9.6 | Cash buyer list from 84K+ records, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | 9.7 | Two-Bucket ARV, disclosure/non-disclosure routing (12 states), adjustments verified against `comp_analyzer.py` |
| 5 | `rehab-estimator.skill` | Deal Analysis | 9.8 | 912-line skill, complete Repair Cheat Sheet verified against real contractor SOW, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | 9.6 | Combined comp+rehab pipeline, MAO (75%/70% rules), multi-loan financing, exit strategy comparison |
| 7 | `deep-prospecting.skill` | Deal Analysis | 9.6 | 4-level research depth (L1-L4), heir verification loop, DOD sanity check (3yr), 3-site skip trace waterfall |
| 8 | `probate-property-finder.skill` | Deal Analysis | 9.7 | Property lookup for probate decedents, 3-tier search (Tax API→Executor→People search), confidence scoring |
| 9 | `phone-validator.skill` | Operations | 9.8 | Trestle API scoring, 5-tier dial priority, 3 tier strategies, litigator risk check, 4.75x connect rate |
| 10 | `sequential-presets.skill` | Operations | 9.5 | 12 niche + 9 bulk filter presets, Pendulum Theory (SMS→Call→Mail→DP), DataSift UI implementation steps |
| 11 | `sift-sequences.skill` | CRM | 9.5 | 26 TCA sequence templates (verified against `sequence_templates.py`), UI walkthrough, HOT A01-A16 chains |
| 12 | `sift-operations.plugin` | CRM | 9.3 | CRM operations encyclopedia, STABM routine, lead pipeline (9 statuses), task presets, team roles |
| 13 | `playbook-creator.skill` | Operations | 9.5 | Playbook/SOP generator from transcripts, 7-node chart limit, 5th grade reading level, Word doc output |

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Preset folders:** "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- **DOD sanity:** MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- **Notice types:** 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

### Key Corrections Made During Optimization (April 2026)
- **Hardcoded credentials removed** from sift-market-research (had email/password in SKILL.md)
- **Bedroom adjustment corrected** from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- **HML points corrected** from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- **Linux paths fixed** in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- **Preset names aligned** across 3 skills to match `niche_sequential.py` source code
- **Transfer tax labeled** as Tennessee-specific in deal-analyzer with state reference table for top 10 states
- **"Substantial renovation" defined** in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

### Skill File Structure
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```
