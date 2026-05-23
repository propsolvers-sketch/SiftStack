# Technology Stack

**Analysis Date:** 2026-04-30

## Languages

**Primary:**
- Python 3.12 — entire backend, scrapers, enrichment pipeline, CLI tools (`src/*.py`)

**Secondary:**
- HTML/CSS — output reports (PDF via `reportlab`, Excel via `openpyxl`)
- JavaScript (executed inside Playwright contexts) — DataSift UI automation, ASP.NET form interaction (`src/datasift_uploader.py`, `src/scraper.py`, `src/extract_market_finder.py`)

## Runtime

**Environment:**
- Python 3.12 (pinned in `Dockerfile` via base image `apify/actor-python-playwright:3.12`)
- Local dev observed running Python 3.14.4 (system); project venv is `.venv/`
- Async runtime: `asyncio` (the scraper, photo importer, DataSift uploader, and Birmingham Accela adapter all use `playwright.async_api`)

**Package Manager:**
- pip (`pip install -r requirements.txt`)
- No lockfile (`requirements.txt` uses `>=` version constraints, no `pip-compile` lock)

**Deployment Runtime:**
- Apify Actor (cloud) — base image `apify/actor-python-playwright:3.12` with Chromium pre-installed (`Dockerfile`)
- `apify` SDK auto-detects environment via `APIFY_IS_AT_HOME` / `APIFY_TOKEN` and routes through `Actor.get_input()` → `Actor.push_data()` (`src/main.py:126-200`)

## Frameworks

**Core:**
- `playwright>=1.40.0` — browser automation for ASP.NET WebForms scrapers, CAPTCHA flow, DataSift CRM upload, Accela Citizen Access portal, Market Finder extraction, Ancestry.com
- `apify>=2.0.0` — Apify Actor SDK; runs the daily pipeline as a scheduled cloud Actor with input schema + dataset output
- `httpx` (transitive via `apify` / direct) — direct REST clients for Madison AssuranceWeb, Jefferson E-Ring Capture, Jefferson tax-table HTML, Madison delinquent JSON, Birmingham address parsing
- `requests>=2.x` (transitive) — synchronous HTTP for Zillow (OpenWeb Ninja), Trestle, Tracerfy, Knox tax API, Slack webhooks, Nominatim, Huntsville PDF download, Firecrawl, Serper

**Testing:**
- No formal test runner configured (no `pytest.ini`, `setup.cfg`, `pyproject.toml`)
- `tests/` directory and root-level `test_*.py` files contain manual harness scripts that import directly and run against live services (e.g., `test_datasift_upload.py`, `test_ancestry.py`, `test_phone_validator.py`)

**Build/Dev:**
- `apify-cli` (npm) — local Actor testing (`apify run --purge`) and deployment (`apify push`)
- Docker — Apify build pipeline reads `Dockerfile` automatically
- `python-dotenv>=1.0.0` — loads `.env` at process start (`src/config.py:9-11`)

## Key Dependencies

**Critical:**
- `playwright>=1.40.0` — primary browser engine; required for all CAPTCHA-gated and SPA-driven sources
- `2captcha-python>=1.2.0` — reCAPTCHA v2 solver wrapper (`src/captcha_solver.py:17`)
- `anthropic>=0.40.0` — Claude Haiku LLM (default `claude-haiku-4-5-20251001`) for notice parsing, obituary search, entity research, photo OCR cleanup (`src/llm_client.py:64-84`)
- `smartystreets-python-sdk>=4.11.0` — USPS address standardization, ZIP+4, geocoding, RDI / vacancy detection (`src/address_standardizer.py:13-20`)
- `dropbox>=12.0.2` — courthouse photo polling + run-summary file uploads (post-Jan-2026 SDK requirement noted in `CLAUDE.md`)
- `google-api-python-client>=2.0.0` + `google-auth>=2.0.0` — optional Google Drive backup uploads via service account (`src/drive_uploader.py:9-22`)
- `apify>=2.0.0` — Actor SDK; runs entire pipeline as a scheduled cloud job

**OCR / Image / PDF:**
- `pypdfium2>=4.0.0` — PDF rendering for tax-sale PDFs and Madison foreclosure newspaper PDFs (`src/pdf_importer.py`, `src/notice_parser.py`)
- `pdfminer.six>=20221105` — text-layer PDF extraction (Madison newspaper PDFs, Huntsville Unsafe Buildings list)
- `pytesseract>=0.3.10` — Tesseract OCR wrapper (`src/image_utils.py`)
- `Pillow>=10.0.0` — image manipulation primitive
- `opencv-python-headless>=4.13.0` — courthouse photo preprocessing (bilateral filter, Otsu threshold, perspective correction). Headless variant chosen to save ~26 MB in Docker (`src/photo_importer.py`)
- `numpy>=1.26.0` — required by OpenCV
- Tesseract binary — must be installed at OS level; binary detection in `src/image_utils.py`

**HTML / Web:**
- `beautifulsoup4>=4.12.0` — TPAD HTML scrape (`src/property_lookup.py:18`), Jefferson tax-table parsing (`src/jefferson_tax_delinquent_api.py:42`)
- `ddgs>=9.0.0` — DuckDuckGo Search wrapper for entity research and obituary discovery (`src/entity_researcher.py:20`)

**Output / Reporting:**
- `openpyxl>=3.1.0` — Excel workbook generation (Market Finder reports, comp analysis, buyer prospecting, market analyzer)
- `reportlab>=4.0.0` — PDF deep prospecting report generation (`src/report_generator.py`)

**Optional / Pluggable:**
- OpenAI-compatible client (transitive via `anthropic` deps) — used to talk to local Ollama (`http://localhost:11434/v1/`) and OpenRouter as alternative LLM backends (`src/llm_client.py:117-304`)

## Configuration

**Environment:**
- `.env` (gitignored) — all credentials and secrets. Template at `.env.example`
- `.env.example` documents every supported env var across data source, CRM, enrichment APIs, skip trace, notifications, Dropbox, optional LLM backends, optional Google Drive, optional Ancestry
- Loaded by `src/config.py:11` via `dotenv.load_dotenv()` at import time
- Apify Actor input (`.actor/input_schema.json`) overrides env vars at runtime when running in Apify (`src/main.py:147-164` syncs Actor input → `config.*` and `os.environ`)

**Key configs required:**
- Credentials: `CAPTCHA_API_KEY`, `DATASIFT_EMAIL`, `DATASIFT_PASSWORD`, `ANTHROPIC_API_KEY`, `SMARTY_AUTH_ID` + `SMARTY_AUTH_TOKEN`, `OPENWEBNINJA_API_KEY`, `TRACERFY_API_KEY`, `TRESTLE_API_KEY`, `SERPER_API_KEY`, `FIRECRAWL_API_KEY`, `SLACK_WEBHOOK_URL`
- Dropbox auto-poll: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` (or short-lived `DROPBOX_ACCESS_TOKEN`), `DROPBOX_POLL_INTERVAL` (default 900s), `DROPBOX_ROOT_FOLDER`
- LLM swapping: `LLM_BACKEND` (`anthropic` | `ollama` | `openrouter`), `LLM_MODEL`, `OLLAMA_MODEL`, `OLLAMA_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_BASE_URL`
- Optional: `GOOGLE_DRIVE_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_KEY` (base64-encoded), `ANCESTRY_EMAIL`, `ANCESTRY_PASSWORD`, `BLUR_THRESHOLD`, `FIRECRAWL_BUDGET`

**Build:**
- `Dockerfile` — single-stage; copies `requirements.txt`, installs deps, copies `src/` and `.actor/`, sets `PYTHONPATH=/home/myuser/src`, runs `python src/main.py`
- `.actor/actor.json` — Apify Actor manifest; declares dataset views, default fields, version, build tag
- `.actor/input_schema.json` — Apify input schema (24 fields incl. mode, county filters, all API keys, pipeline toggles)
- `.apifyignore` — excludes from Actor build artifact

**Runtime State Files (gitignored):**
- `last_run.json` — daily-mode high-water-mark (`src/config.py:17`)
- `seen_ids.json` — notice ID dedup with 90-day prune (`src/config.py:18-19`)
- `captcha_failed_ids.json` — exhausted-retry IDs with 14-day prune (`src/config.py:23-24`)
- `cookies.json` — site session reuse (`src/config.py:25`)
- `dropbox_state.json` — Dropbox cursor for incremental polling (`src/config.py:26`)
- `photo_state.json` — processed photo dedup
- `datasift_cookies.json` — DataSift session reuse
- `.ancestry_profile/` — persistent Playwright user-data-dir for Ancestry login
- `.ancestry_page_loads.json` — daily 100-page load counter for account-protection circuit breaker

## Platform Requirements

**Development:**
- Python 3.12+ (project Dockerfile pins 3.12; system can run 3.14)
- `pip install -r requirements.txt`
- `playwright install chromium` (Chromium browser binaries — required for all Playwright-backed code paths)
- Tesseract binary installed at OS level (for `pytesseract` OCR); auto-detected in `src/image_utils.py`
- Optional: Ollama installed locally if using `LLM_BACKEND=ollama`

**Production:**
- **Apify Actor cloud** — primary deployment target. Base image `apify/actor-python-playwright:3.12` ships with Chromium + Tesseract preinstalled. Daily schedule configured in Apify Console; secrets passed via Actor input
- Docker-compatible runner — image builds from `Dockerfile`; works on any container host
- Dropbox-watch mode (`python src/main.py dropbox-watch`) is a long-running poll loop, suitable for a persistent worker (Apify, systemd, supervisor, etc.)

---

*Stack analysis: 2026-04-30*
