# External Integrations

**Analysis Date:** 2026-04-30

SiftStack is integration-heavy by design — it ingests from public-record sites, enriches through commercial APIs, and pushes results into a CRM and notification stack. There is **no internal database**: state lives in flat JSON files, and the source-of-truth datastore is the DataSift.ai CRM.

## APIs & External Services

### Public-Notice / Source-of-Record Scrapers

**Alabama Public Notices (alabamapublicnotices.com):**
- Purpose: ASP.NET WebForms search portal for AL foreclosure, probate, code-violation publications (Jefferson + Madison counties)
- Base URL: `https://www.alabamapublicnotices.com` (`src/config.py:69`)
- Search URL: `${BASE_URL}/Search.aspx`
- Auth: None for search; reCAPTCHA v2 on every notice detail page
- Client: Playwright (`src/scraper.py:9`)
- ASP.NET selectors and reCAPTCHA sitekey hardcoded in `src/config.py:73-90` (sitekey `6LccnQ8sAAAAAMNFrb4ZLDtPAqk50k_r-CCwimHJ`)
- Rate limit: 2-3 second random delays, max 3 retries per page (`src/config.py:93-95`)

**Tennessee Public Notice (tnpublicnotice.com):**
- Purpose: Legacy data source; replaced by alabamapublicnotices.com but credentials still in `.actor/input_schema.json` (`tn_username`, `tn_password`)
- Env vars: `TNPN_EMAIL`, `TNPN_PASSWORD` (used in Apify Actor mode at `src/main.py:184`)

**2Captcha (api.2captcha.com):**
- Purpose: solves reCAPTCHA v2 on every notice detail page (~10-30s per solve, ~$2.99/1000 solves)
- Client: `2captcha-python>=1.2.0` SDK (`src/captcha_solver.py:17`)
- Auth: API key (`CAPTCHA_API_KEY`)
- Balance check at preflight: `https://2captcha.com/res.php?key=...&action=getbalance` (`src/main.py:103-118`)
- Pattern: solver receives sitekey + page URL, returns `g-recaptcha-response` token, token is injected into hidden textarea, "View Notice" button is clicked

### Property / Tax Assessor APIs

**Knox County Tax (knox-tn.mygovonline.com):**
- Purpose: Knox County, TN parcel + delinquency lookup; also used as decedent address-search for probate property locator
- Base URL: `https://knox-tn.mygovonline.com/api/v2` (`src/tax_enricher.py:19`, `src/obituary_enricher.py:1405`)
- Auth: None (public API)
- Endpoints: `/parcels/{name_or_address}?detail_level=public`, `/due/PPT/{parcel_id}?detail_level=public`
- Client: `requests` (`src/tax_enricher.py:13`)
- Rate limit: 1-2s random delay between calls (`src/tax_enricher.py:20-21`)

**TPAD — TN Comptroller (Blount County):**
- Purpose: Blount County, TN property assessor data (TN Comptroller-hosted)
- Base URL: `https://assessment.cot.tn.gov/TPAD/Search` (`src/property_lookup.py:24`)
- Jurisdiction code for Blount: `005` (`src/property_lookup.py:25`)
- Auth: None
- Pattern: `requests.get` with query params `Jur` + `Query`, parsed by BeautifulSoup against `#resultsTable`

**KGIS Maps — Knox County GIS:**
- Purpose: Knox County parcel ownership lookup via ArcGIS-backed map
- URLs: `https://www.kgis.org/kgismaps/`, `https://www.kgis.org/parcelreports/ownercard.aspx?id={parcel_id}` (`src/property_lookup.py:97-98`)
- Auth: None (Playwright scrape; ArcGIS REST endpoints would require auth)
- Client: Playwright (async)

**Madison County, AL Property (AssuranceWeb / countygovservices.com):**
- Purpose: Madison County, AL parcel search by owner name or situs address; backs probate property locator + Huntsville unsafe-building owner enrichment
- Base URL: `https://madisonproperty.countygovservices.com` (`src/madison_property_api.py:31`)
- Search endpoint: `/Property/Property/Search`
- Delinquent feed: `/Property/Property/DelinquentParcels` — single GET returns ~600 parcels inline as Kendo Grid JSON (`src/madison_tax_delinquent_api.py:39-41`)
- Auth: None; CSRF token grabbed from search form's `__RequestVerificationToken` and replayed
- Client: `httpx` (`src/madison_property_api.py:27`)
- CLI: `python src/madison_property_api.py SMITH`

**Jefferson County, AL Property (E-Ring Capture / capturecama.com):**
- Purpose: Jefferson County, AL parcel search (backs Birmingham metro probate, foreclosure, and code-violation enrichment); the eringcapture.jccal.org SPA front-door is Imperva/Incapsula-protected, but the underlying REST endpoint accepts direct JSON POST
- API URL: `https://jeffersonexpress.capturecama.com/SearchRP` (`src/jefferson_property_api.py:31`)
- Tenant URL: `https://eringcapture.jccal.org` (carried in POST body)
- Express URL: `https://jeffersonexpress.capturecama.com`
- Auth: None
- Client: `httpx` with `verify=False` (capturecama.com serves an incomplete SSL chain — Python's certifi lacks the intermediate; documented in `src/jefferson_property_api.py`)
- Search types: `1=owner`, `2=parcel`, `3=mailing_address`, `4=property_address` (`src/jefferson_property_api.py:38-41`)
- CLI: `python src/jefferson_property_api.py SMITH`

**Jefferson County, AL Tax Collector (jccal.org):**
- Purpose: official annual tax-lien auction roster (Birmingham + Bessemer divisions); ~18,225 parcels combined as of 2024 publication
- URL pattern: `https://www.jccal.org/Sites/Jefferson_County/Documents/{year}/{Birmingham|Bessemer}TaxTable-{year}.html` (`src/jefferson_tax_delinquent_api.py:54-56`)
- Auth: None
- Client: `httpx` (120s timeout — Birmingham table is ~8.4 MB) + `BeautifulSoup`
- Rate: ~3s wall time for both districts in one pass

**Huntsville Code Enforcement (huntsvilleal.gov):**
- Purpose: monthly Unsafe Building list PDF; ~222 active condemnation cases at any time
- URL pattern: `https://www.huntsvilleal.gov/wp-content/uploads/{YYYY}/{MM}/{MM}-{YYYY}-Unsafe-Building-List.pdf` (`src/huntsville_unsafe_buildings_api.py:43-45`)
- Auth: None
- Client: `requests` (NOT `httpx` — huntsvilleal.gov sits behind a WAF that fingerprints httpx and 403s; documented in `src/huntsville_unsafe_buildings_api.py:123`)
- Parser: `pdfminer.six` for 3-column PDF layout extraction
- Auto-discovery: walks back up to 6 months from current month if requested month isn't published

**Birmingham Code Enforcement (Accela Citizen Access):**
- Purpose: 5 enforcement record categories (Condemnation, Housing, Inoperable Vehicles, Environmental, Zoning); ~300-500 cases/month
- URL: `https://aca-prod.accela.com/BIRMINGHAM/Cap/CapHome.aspx?module=Enforcement&TabName=Enforcement` (`src/birmingham_code_enforcement_api.py:54-57`)
- Auth: None
- Client: Playwright (Accela uses ASP.NET WebForms with `__VIEWSTATE` postbacks — not driveable via plain HTTP)
- Optional detail-page enrichment for owner + fees (~3s/record)

**Smarty (Smarty Streets):**
- Purpose: USPS address standardization, ZIP+4, geocoding, RDI (Residential/Commercial), vacancy detection
- Auth: `SMARTY_AUTH_ID` + `SMARTY_AUTH_TOKEN` (paired BasicAuthCredentials)
- Client: `smartystreets-python-sdk>=4.11.0` (`src/address_standardizer.py:13-20`)
- Batch: 100 lookups per `Batch()` (`src/address_standardizer.py:26`)
- Free tier: 250 lookups/month
- Fallback: when a lookup fails but lat/lon was extracted from notice text, retries via Nominatim reverse-geocode (1 req/sec rate limit) → Smarty (`src/address_standardizer.py:217-355`)

**Nominatim (OpenStreetMap):**
- Purpose: reverse-geocode lat/lon → city/postcode for Smarty retry path
- URL: `https://nominatim.openstreetmap.org/reverse` (`src/address_standardizer.py:189`)
- Auth: None (User-Agent header required: `TN-Notice-Scraper/1.0`)
- Rate limit: 1 req/sec (enforced client-side)

### Property Enrichment & Listings

**OpenWeb Ninja (Real-Time Zillow Data API):**
- Purpose: Zestimate, MLS status, equity, beds/baths/sqft/year_built, price history
- Base URL: `https://api.openwebninja.com/realtime-zillow-data` (`src/property_enricher.py:22`)
- Endpoint: `/property-details-address` (GET)
- Auth: `x-api-key` header (`OPENWEBNINJA_API_KEY`)
- Client: `requests` (`src/property_enricher.py:15`)
- Rate limit: 1-2s random delay between calls; backs off 10s on 429 (`src/property_enricher.py:25-148`)
- Free tier: 100 requests/month

### LLM / AI

**Anthropic (Claude Haiku):**
- Purpose: structured field extraction (notice parsing fallback), obituary search synthesis, entity research, photo OCR cleanup, address extraction from people-search pages
- Default model: `claude-haiku-4-5-20251001` (configurable via `LLM_MODEL`)
- Auth: `ANTHROPIC_API_KEY`
- Client: `anthropic>=0.40.0` (sync + async) (`src/llm_client.py:64-111`)
- Pattern: `chat_json()` / `chat_json_async()` send a prompt + system message, expect JSON response (markdown fence stripping included)

**OpenRouter (alternative LLM backend):**
- Purpose: pluggable cheaper LLM via OpenRouter
- Base URL: `https://openrouter.ai/api/v1` (`src/llm_client.py:220`)
- Default model: `qwen/qwen-2.5-72b-instruct`
- Auth: `OPENROUTER_API_KEY`
- Client: `openai`-compatible (OpenAI Python SDK pointed at OpenRouter base URL)
- Activation: `LLM_BACKEND=openrouter`

**Ollama (local LLM backend):**
- Purpose: free local LLM for development
- Base URL: `http://localhost:11434/v1/` (default; configurable via `OLLAMA_BASE_URL`)
- Default model: `qwen2.5:7b`
- Auth: API key string `"ollama"` (placeholder; Ollama doesn't auth)
- Client: `openai`-compatible (`src/llm_client.py:117-159`)
- Activation: `LLM_BACKEND=ollama`

### Search Engines & Scraping

**Serper.dev (Google Search API):**
- Purpose: Google search for decision-maker / heir mailing-address discovery on people-search sites; obituary URL discovery
- URL: `https://google.serper.dev/search` (`src/obituary_enricher.py:939`)
- Auth: `SERPER_API_KEY`
- Client: `requests`
- Free tier: 2,500 queries

**Firecrawl (JS-rendered scraping):**
- Purpose: scrape JavaScript-rendered people-search results pages (`truepeoplesearch.com`, `fastpeoplesearch.com`, `cyberbackgroundchecks.com`, `peoplefinder.com`, `spokeo.com`, `whitepages.com`)
- URL: `https://api.firecrawl.dev/v1/scrape` (`src/obituary_enricher.py:1026`)
- Auth: `FIRECRAWL_API_KEY`
- Client: `requests`
- Free tier: 500 pages
- Budget management: `FIRECRAWL_BUDGET` env var (default 3000) — hard stops at exhaustion (`src/obituary_enricher.py:989-1053`)

**DuckDuckGo Search:**
- Purpose: free fallback search for obituary discovery + entity research
- Client: `ddgs>=9.0.0` (`src/entity_researcher.py:20`)
- Auth: None

**Web Archive (web.archive.org):**
- Purpose: fallback fetch when origin pages are gone
- URL pattern: `https://web.archive.org/web/2/{url}` (`src/obituary_enricher.py:524`)
- Auth: None

**People Search Sites (scraped):**
- Domains: `truepeoplesearch.com`, `fastpeoplesearch.com`, `cyberbackgroundchecks.com`, `peoplefinder.com`, `spokeo.com`, `whitepages.com` (`src/obituary_enricher.py:759-766`)
- Pattern: Serper Google search → Firecrawl render → Claude Haiku extracts address
- No direct auth; defensive UA + delays

### Skip Trace & Phone Validation

**Tracerfy (skip trace):**
- Purpose: batch skip trace producing phones (up to 9) + emails (up to 5) per record
- URLs: `https://tracerfy.com/v1/api/trace/`, `https://tracerfy.com/v1/api/queue/{queue_id}`, `https://tracerfy.com/v1/api/trace/lookup/` (`src/tracerfy_skip_tracer.py:31-32`, `src/obituary_enricher.py:1156-1279`)
- Auth: `TRACERFY_API_KEY`
- Client: `requests`
- Pricing: $0.02/record
- Pattern: POST batch → poll `/queue/{id}` → fetch results

**Trestle (phone scoring):**
- Purpose: 5-tier dial-priority scoring (Dial First through Drop) + optional litigator-risk flag
- Endpoint: `https://api.trestleiq.com/3.0/phone_intel` (`src/phone_validator.py:52`)
- Auth: `TRESTLE_API_KEY`
- Client: `requests` with `ThreadPoolExecutor` parallelism (`src/phone_validator.py:28`)
- Pricing: $0.015/phone
- Tiers (`src/phone_validator.py:40-46`): Dial First 81-100, Dial Second 61-80, Dial Third 41-60, Dial Fourth 21-40, Drop 0-20

**Ancestry.com:**
- Purpose: SSDI (Social Security Death Index, 89M+ records), Ancestry obituary collection, Newspapers.com index (930M+ pages, recent TN papers via All-Access SSO)
- URLs: `https://www.ancestry.com`, `https://www.ancestry.com/account/signin`, `https://www.newspapers.com/search/?...` (`src/ancestry_enricher.py:30-31, 658`)
- Auth: `ANCESTRY_EMAIL`, `ANCESTRY_PASSWORD`; requires World Explorer subscription (~$29/month)
- Client: Playwright with **persistent profile** (`.ancestry_profile/` user-data-dir) and aggressive bot-detection protection
- Hard limit: 100 page loads/day (`src/ancestry_enricher.py:35`); circuit-breaker on any bot-detection signal
- Human-like delays 2-5s between actions

## Data Storage

**Databases:**
- **None.** No internal SQL/NoSQL database. State lives in flat JSON files in project root (gitignored)
- State files: `last_run.json`, `seen_ids.json`, `captcha_failed_ids.json`, `cookies.json`, `dropbox_state.json`, `photo_state.json`, `datasift_cookies.json`, `.ancestry_page_loads.json` (paths in `src/config.py:13-27`)
- Atomic write helpers: `config.save_state()` / `config.load_state()` write tmp → rename with `.bak` fallback on read (`src/config.py:314-341`)
- Output data: CSVs in `output/` (gitignored); per-record PDFs in `output/reports/`

**Source-of-Truth Datastore:**
- **DataSift.ai CRM** — the canonical record store. SiftStack writes; DataSift owns lifecycle, sequences, status, marketing
- 80-column CSV schema documented in `CLAUDE.md` and implemented in `src/datasift_formatter.py`

**File Storage:**
- **Dropbox** — courthouse photo intake (autopolled); CSV + deep-prospecting PDF distribution via shared links in Slack notifications
- **Google Drive** (optional) — CSV + summary text backup uploaded via service account
- **Apify key-value store** — `output.csv` backup when running as Actor (`src/main.py` Actor branch)
- **Apify dataset** — structured records pushed via `Actor.push_data()` for Apify Console viewing

**Caching:**
- None — no Redis, Memcached, or in-process LRU. The Firecrawl module tracks call counts in-process for budget enforcement only (`src/obituary_enricher.py:989-1053`)

## Authentication & Identity

**Auth Provider:**
- **No internal auth.** SiftStack is single-tenant; credentials are environment-injected and per-process
- All third-party auth is API-key-based via env vars, with two exceptions:
  - Dropbox uses OAuth2 (refresh-token flow with auto-rotating access tokens)
  - DataSift / Ancestry use email + password Playwright login (cookies persisted to disk for session reuse)
- Google Drive uses base64-encoded service-account JSON key (no user OAuth)

**Auth Mechanisms by Service:**
| Service | Mechanism | Env Vars |
|---|---|---|
| 2Captcha | API key (URL param) | `CAPTCHA_API_KEY` |
| Smarty | BasicAuthCredentials | `SMARTY_AUTH_ID`, `SMARTY_AUTH_TOKEN` |
| OpenWeb Ninja | `x-api-key` header | `OPENWEBNINJA_API_KEY` |
| Anthropic | `Authorization: Bearer` | `ANTHROPIC_API_KEY` |
| OpenRouter | `Authorization: Bearer` | `OPENROUTER_API_KEY` |
| Serper | `X-API-KEY` header | `SERPER_API_KEY` |
| Firecrawl | `Authorization: Bearer` | `FIRECRAWL_API_KEY` |
| Tracerfy | API key | `TRACERFY_API_KEY` |
| Trestle | API key | `TRESTLE_API_KEY` |
| Slack/Discord | Webhook URL | `SLACK_WEBHOOK_URL` |
| Dropbox | OAuth2 refresh token | `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` (or `DROPBOX_ACCESS_TOKEN` short-lived) |
| Google Drive | Service account (base64 JSON) | `GOOGLE_SERVICE_ACCOUNT_KEY`, `GOOGLE_DRIVE_FOLDER_ID` |
| DataSift.ai | Email + password (Playwright) | `DATASIFT_EMAIL`, `DATASIFT_PASSWORD` |
| Ancestry.com | Email + password (Playwright + persistent profile) | `ANCESTRY_EMAIL`, `ANCESTRY_PASSWORD` |
| alabamapublicnotices.com | None (CAPTCHA-gated) | — |
| Knox / Madison / Jefferson APIs | None | — |

## Monitoring & Observability

**Error Tracking:**
- No formal error-tracker (no Sentry, Rollbar, etc.)
- Errors are logged to `logs/` with timestamped filenames + sent to Slack/Discord via `src/slack_notifier.py:notify_error()`

**Logs:**
- Python `logging` module, INFO-level by default, DEBUG with `-v` flag
- Output: `logs/` directory (gitignored), timestamped filenames
- Apify Actor: logs via `Actor.log` go to Apify Console live log stream

**Notifications:**
- **Slack / Discord** webhook (single env var, dual-purpose):
  - URL: `SLACK_WEBHOOK_URL`
  - Discord must use the `/slack` suffix: `https://discord.com/api/webhooks/{id}/{token}/slack`
  - Implementation: `src/slack_notifier.py` — `notify_error()`, `notify_warning()`, daily run summary with cost breakdown
  - Sends: pipeline errors, preflight failures, daily run summaries (record counts, cost breakdown, Dropbox share links to CSV + PDF)

## CI/CD & Deployment

**Hosting:**
- **Apify Actor cloud** — primary production deployment
- Actor name: `tn-public-notice-scraper`, version `1.0`, build tag `latest` (`.actor/actor.json:3-7`)
- Schedule + secrets configured in Apify Console
- Built from `Dockerfile` automatically on `apify push`

**CI Pipeline:**
- No GitHub Actions / CircleCI / GitLab CI workflows present in repo
- Deployment is manual: `apify login` → `apify push`
- Local Actor test: `apify run --purge` (reads `input.json`, simulates Actor environment)

**Container:**
- Base image: `apify/actor-python-playwright:3.12` (Chromium pre-installed)
- `Dockerfile` is single-stage (no multi-stage build)
- Working dir: `/home/myuser/`; `PYTHONPATH=/home/myuser/src`

## Environment Configuration

**Required env vars (full pipeline):**
- `CAPTCHA_API_KEY`, `ANTHROPIC_API_KEY`
- `SMARTY_AUTH_ID`, `SMARTY_AUTH_TOKEN`
- `OPENWEBNINJA_API_KEY`
- `DATASIFT_EMAIL`, `DATASIFT_PASSWORD`
- `TRACERFY_API_KEY`, `TRESTLE_API_KEY`
- `SERPER_API_KEY`, `FIRECRAWL_API_KEY`
- `SLACK_WEBHOOK_URL`

**Required env vars (mode-specific):**
- `dropbox-watch` mode: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`
- `phone-validate` mode: `TRESTLE_API_KEY` (hard-required, not optional)
- Apify Actor mode: `tn_username`, `tn_password` (legacy TN credentials still in input schema)

**Optional env vars:**
- `GOOGLE_DRIVE_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_KEY` (CSV/summary backup)
- `ANCESTRY_EMAIL`, `ANCESTRY_PASSWORD` (SSDI / obituary collection)
- `LLM_BACKEND`, `LLM_MODEL`, `OLLAMA_MODEL`, `OLLAMA_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (LLM backend swapping)
- `DROPBOX_POLL_INTERVAL` (default 900s), `DROPBOX_ROOT_FOLDER`
- `BLUR_THRESHOLD` (default 100; OCR rejection threshold)
- `FIRECRAWL_BUDGET` (default 3000)

**Secrets location:**
- Local: `.env` (gitignored, template at `.env.example`)
- Production: Apify Actor input fields marked `isSecret: true` in `.actor/input_schema.json` — values stored encrypted in Apify Console, injected at Actor run time

## Webhooks & Callbacks

**Incoming:**
- **None.** SiftStack does not expose any HTTP endpoints; it's a batch / poll worker
- Closest thing: Dropbox folder polling (`dropbox-watch` mode) — pull-based with cursor state, not push webhooks

**Outgoing:**
- **Slack / Discord webhook** (single endpoint, dual-purpose) — see Monitoring section
- **Apify Actor dataset / key-value store push** — when running as Actor, results are pushed via `Actor.push_data()` (dataset) and key-value store (CSV backup)
- **Dropbox shared-link generation** — `src/dropbox_uploader.py:_ensure_shared_link()` creates public URLs for output CSV + PDF reports, embedded in Slack run summaries
- **DataSift Playwright upload** — Playwright-driven UI automation against `https://app.reisift.io/*` (login → upload wizard → enrich → skip trace → manage presets → SiftMap sold tagging). All URLs in `src/datasift_core.py:21-25`. There is **no DataSift REST API** — all interactions are browser automation against the SPA

---

*Integration audit: 2026-04-30*
