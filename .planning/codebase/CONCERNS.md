# Codebase Concerns

**Analysis Date:** 2026-04-30

This audit was performed against the working tree on `main` with 12 modified files and 11 untracked files staged for an upcoming AL-pipeline commit. The whole-codebase port from the TN counties (Knox/Blount on tnpublicnotice.com) to AL counties (Jefferson/Madison on alabamapublicnotices.com) is in flight today; many of the concerns below are direct fallout from that migration.

## In-Flight (Uncommitted) Changes — 2026-04-30

These changes are working but unreviewed and unmerged. They block any clean release.

**Modified, unstaged (12 files, +2,204 / −712):**
- `src/scraper.py` — wholesale rewrite from TN Smart-Search login flow to AL keyword-search form (+412/−523). Removes `LOGIN_URL`, `COOKIES_FILE`, `SEL_LOGIN_*`, `SEL_SAVED_SEARCHES_DROPDOWN`, `TNPN_EMAIL/PASSWORD`. **Largest net change in the working tree.**
- `src/notice_parser.py` (+730 lines, −0 net) — adds full AL probate metadata extraction (`PROBATE_NAME_GRANTED_RE`, `PROBATE_NAME_BEFORE_TITLE_RE`, `CASE_NUMBER_RE`, `JUDGE_RE`, `GRANTED_DATE_RE`, probate-subtype detection, `_parse_probate_metadata`, etc.).
- `src/llm_parser.py` — system prompt + every per-type prompt template flipped from "Tennessee/TN" to "Alabama/AL"; foreclosure prompt extended with `mortgage_company`/`original_lender`/`trustee`/`trustee_file_number`; probate prompt adds `case_number`/`judge_name`/`granted_date`. `MAX_TOKENS` raised 256→512.
- `src/foreclosure_filter.py` — adds 6 AL include phrases. **Removes** `"substitute trustee's notice of sale"` (TN-style) from the include list — verify TN photo-import / re-import paths don't regress.
- `src/property_lookup.py` — new Jefferson/Madison branch delegates to `probate_property_locator.enrich_notice_with_property` for AL counties; Knox/Blount paths preserved.
- `src/enrichment_pipeline.py` — vacant-land filter and `_validate_records()` exempt `{probate, divorce}` (probate has no property address until tax-roll lookup matches).
- `src/data_formatter.py` — adds 32 columns to the `SIFT_COLUMNS` schema (final-pass probate columns). Existing CSV consumers downstream of this format will break if they parse by position.
- `src/datasift_formatter.py` (+225) — adds 7 AL probate enrichment columns + tax dollar-exposure tags (`tax_high_exposure`, `tax_high_exposure_10k`, `individual_owner`, `entity_owned`) + AL probate filter-preset tags (`municipality_*`, `homestead`, `probate_sale`, `multi_parcel`, `hearing_upcoming`, `creditor_window_open`).
- `src/main.py` — preflight checks updated, but **see "Apify Cold-Start Crash" below**.
- `src/config.py` — replaces `TNPN_EMAIL/PASSWORD` with `APNA_EMAIL/PASSWORD`, retargets `BASE_URL`/`SEARCH_URL`, adds `SearchConfig`, populates `SAVED_SEARCHES` with 14 Jefferson/Madison entries.
- `requirements.txt` — adds `pypdfium2>=4.0.0`.

**Untracked (11 files), all part of the AL pipeline:**
- `src/birmingham_code_enforcement_api.py` (582 lines)
- `src/code_violation_pipeline.py`
- `src/huntsville_unsafe_buildings_api.py`
- `src/jefferson_property_api.py`, `src/jefferson_tax_delinquent_api.py`
- `src/madison_property_api.py`, `src/madison_tax_delinquent_api.py`
- `src/probate_property_locator.py`
- `src/tax_distress_pipeline.py`
- `docs/foia/huntsville_code_enforcement_request.md`

These need a coordinated commit (or split commits along the four pipelines: probate-property-locator, tax-distress, code-violation, AL scraper migration). The longer they sit uncommitted, the higher the risk of drift between the working files and CLAUDE.md's now-extensive AL documentation.

**Backup files in repo root** — `last_run.json.backup_pre_rerun`, `seen_ids.json.backup_pre_rerun` are operator artifacts from manual reruns. They should be in `.gitignore` (the `.bak` versions already are; the `.backup_pre_rerun` extension isn't covered).

---

## Critical Bugs (in-flight code)

### Apify Actor cold-start `AttributeError`

`src/main.py:184` references `config.TNPN_EMAIL` / `config.TNPN_PASSWORD`. Those names were deleted from `config.py` in this same diff (replaced by `APNA_EMAIL`/`APNA_PASSWORD`). The CLI path was correctly updated at `src/main.py:60-66`; the Actor path was missed.

```python
# src/main.py line 184 (in actor_main()):
if not config.TNPN_EMAIL or not config.TNPN_PASSWORD:
    Actor.log.error("tn_username and tn_password are required")
```

The Actor `_cred_map` at `src/main.py:147-160` also no longer maps `tn_username`/`tn_password` from Actor input — but the validator still checks for them. **Result:** every Apify scheduled run will crash on this attribute access before it ever reaches the scrape, regardless of which credentials are in the Actor input.

**Fix:** delete lines 184-192 entirely (AL site needs no login — only `CAPTCHA_API_KEY`). The CAPTCHA check at line 193 already exists.

**Impact:** any Apify deployment of this branch is dead-on-arrival. Local CLI runs are unaffected.

### Madison probate-property locator passes the wrong name

`src/probate_property_locator.py:149-163` (`_search_madison`) takes the decedent name "FIRST MIDDLE LAST" (the format `notice.decedent_name` is set to throughout the codebase, e.g. "MARY ANGELA SMITH") and calls:

```python
parts = name.strip().split()
last = parts[0] if parts else ""
first = " ".join(parts[1:]) if len(parts) > 1 else None
return search_by_owner_name(last, first or None)
```

The Madison adapter (`src/madison_property_api.py:179`) signature is `search_by_owner_name(last_name: str, first_name: str | None)` — **so this queries Madison with `last_name="MARY", first_name="ANGELA SMITH"`**. The assessor stores names "LAST, FIRST MIDDLE" and prefix-matches on the first criteria, so this will mostly return zero hits.

The Jefferson adapter (`_search_jefferson` at line 120) has the symmetric reorder problem solved: it tries the original form, then retries with the last token moved to the front. Madison has no such retry.

**Fix:** in `_search_madison`, try both `(parts[0], " ".join(parts[1:]))` AND `(parts[-1], " ".join(parts[:-1]))` and concatenate the result lists, mirroring Jefferson's belt-and-suspenders pattern. Better yet: extract a single `_normalize_name_for_assessor()` helper and feed it from both sides.

**Impact:** AL probate property-address enrichment for Madison currently silently returns 0 hits in nearly all cases. The PR-name fallback (Tier 2) papers over this when the surviving spouse is the PR (because the PR's recorded name actually IS in "LAST FIRST" form on the tax roll), but pure-decedent matches will fail. **Madison probate property-locator hit rate is degraded relative to Jefferson without anyone noticing yet — there's no metric for it.**

### `_GARBAGE_RE` doesn't do what the docstring says

`src/enrichment_pipeline.py:214` defines:

```python
_GARBAGE_RE = re.compile(r"^[^a-zA-Z0-9]*$")
```

The docstring at line 226 says `address must contain at least one letter (not pure garbage OCR)`. The regex actually matches strings that are **entirely non-alphanumeric** (e.g. `"---"` or `""`). **Numeric-only addresses pass validation** (e.g. an OCR'd address that came out as just `"12345"` — a parcel number leaked into the address slot) because `5` is alphanumeric, so the regex doesn't match.

**Impact:** OCR'd photo records where the address parser pulled a parcel ID into the address slot will pass validation and ship to DataSift as garbage. Low frequency but present.

**Fix:** change to `re.compile(r"^[^a-zA-Z]*$")` (at least one **letter**, matching the docstring), or add a parallel "must contain at least one digit" check so the bug is symmetric.

---

## Documented-but-Unfixed Parser Fragility

### `PR_ADDRESS_RE` is name-after-title only

`src/notice_parser.py:602-617`. The regex anchors on the **PR title** keyword and then captures the address after it:

```
(Personal Representative|Executor|...) <name + title suffix> <address>
```

In Alabama signature blocks, the format is:

```
JOHN SMITH
Personal Representative
123 Main St
Birmingham, AL 35203
```

— **name first, title second, address third**. The current regex requires the title to appear before the address, so it never matches. CLAUDE.md acknowledges this ("Documented but not fixed. Without LLM fallback this drops AL probate records") and the LLM fallback in `llm_parser.py` does fill `owner_street`/`owner_city`/`owner_state`/`owner_zip` for AL probate notices.

**Impact:** when `ANTHROPIC_API_KEY` is unset (or Claude Haiku is rate-limited), AL probate records will systematically lack PR mailing addresses. Combined with the just-shipped validator exemption, those records still pass validation but are unmailable. They will surface in DataSift as records-without-contact-paths and cause filter-preset noise.

**Fix:** add a second regex `PR_ADDRESS_NAME_FIRST_RE` that matches `<name>\n(Personal Representative|Executor|...)\n<address>`. `_parse_pr_address()` at line 1689 should try both patterns.

### Probate name extraction has 3 patterns checked sequentially

`notice_parser.py:1373` tries `PROBATE_NAME_GRANTED_RE`, then `PROBATE_NAME_BEFORE_TITLE_RE`, then `PROBATE_NAME_RE`. The third pattern is the legacy TN format (`"Personal Representative: NAME"`). It's kept "for backward compat" but no probate notices on alabamapublicnotices.com use that format. The cost is low (regex match attempt on already-extracted text), but it's dead code that complicates reasoning about which pattern matched in production logs.

### County detection logic mentions Knox/Blount in docstring

`notice_parser.py:780-790` — `is_target_county()` docstring still says "Returns True if the property appears to be in Knox or Blount County" but `_TARGET_COUNTIES` at line 777 is `{"jefferson", "madison"}`. Stale docstring. The behavior is correct; the comment is misleading.

### `captcha_solver.py` docstring also stale

`src/captcha_solver.py:1` docstring says "2Captcha integration for solving reCAPTCHA v2 on tnpublicnotice.com." Functionally fine — the sitekey comes from `config.RECAPTCHA_SITEKEY` which is correct for AL — but a future maintainer reading the file to debug AL captcha issues will be momentarily confused.

---

## Filter Over-Cull Risk (parallel to the just-fixed vacant-land bug)

The just-shipped fix in `enrichment_pipeline.py` exempts `{probate, divorce}` from the **vacant-land filter** and the **data validator**. Two other filters in the same file have the same structural setup but were NOT exempted — and have the same potential for false-positive culling:

### `_filter_commercial` (line 178-189)

```python
result = [n for n in notices if n.rdi.lower() != "commercial"]
```

Smarty's RDI flags any address it classifies as commercial. For a probate decedent whose property is a mixed-use building or whose tax-roll address is the PR's law office (rare but observed in test data), this drops the record. The PR mailing address is still mailable. **No probate/divorce exemption.**

**Recommended:** add the same `_NO_PROPERTY_ADDRESS_TYPES` exemption used by the vacant-land and validation filters.

### `_filter_entity_owners` (line 136-175)

Drops records where `tax_owner_name` or `owner_name` matches `BUSINESS_RE`. For probate, `owner_name` is the PR (a person, by definition), so this is mostly safe. Edge case: if the property locator wrote a corporate trustee back into `tax_owner_name` (e.g. "FIRST AMERICAN TITLE"), the entity filter would drop the record. The trust/estate exemptions at lines 151-154 cover most personal-trust cases but not all.

**Lower priority** than the commercial filter, but worth wiring the exemption symmetrically.

### Step 3c probate property lookup is Knox-only — but in the WRONG way

`enrichment_pipeline.py:388-394`:

```python
probate_no_addr = [
    n for n in notices
    if n.notice_type == "probate"
    and not n.address.strip()
    and n.decedent_name.strip()
    and n.county.lower() == "knox"
]
```

This Knox-only check predates the AL pipeline. The just-added AL property-locator runs **upstream** of `run_enrichment_pipeline` (in `main.py:332` and `:1727` — `lookup_decedent_properties` is called BEFORE `run_enrichment_pipeline`), so AL probate records get their addresses populated before reaching this step. That's correct behavior, but it leaves Step 3c as Knox-only dead-feeling code. A future maintainer adding a third county (e.g. Shelby) will likely look at this filter, see Knox-only, and add their county here — which would be wrong (the AL pattern is to do property lookup upstream of the pipeline).

**Recommended:** either delete Step 3c entirely (and explicitly require `lookup_decedent_properties` to be called by every entry point), or make Step 3c the canonical place and remove the upstream calls in `main.py`.

Same comment applies to `enrichment_pipeline.py:412` (Step 4 parcel address lookup): `n.county.lower() == "knox"` — Knox-only.

---

## External-Service Reliance (single points of failure)

The pipeline depends on **9+ external paid services**, most without graceful degradation:

| Service | Where | Failure mode | Cost-per-call |
|---|---|---|---|
| **2Captcha** | `src/captcha_solver.py` | Required on every notice detail page. If 2Captcha is down or the balance check (preflight in `main.py:100-118`) fails, the daily run aborts. | $0.003 each, ~10-30s latency |
| **Anthropic Claude Haiku** | `src/llm_parser.py`, `src/obituary_enricher.py`, `src/entity_researcher.py`, deep prospecting | LLM fallback is "soft" (regex passes still produce records) but AL probate PR mailing addresses will be missing — see PR_ADDRESS_RE concern. | ~$0.001-0.005/notice |
| **Smarty (USPS)** | `src/address_standardizer.py` | Soft — preflight downgrades to a warning. No address standardization → no zip+4 → DataSift rejects some records. | Free tier capped |
| **OpenWebNinja Zillow** | `src/property_enricher.py` | Soft — warning. No equity/MLS data → "Off Market" auction-listing fix becomes irrelevant since no listings get fetched. | Per-call |
| **Tracerfy** | `src/tracerfy_skip_tracer.py` | Hard for the Apify deep-prospecting subset; soft otherwise. | $0.20/match |
| **Trestle** | `src/phone_validator.py` | Soft — phone tier badges absent in PDFs. | Per-call |
| **Serper.dev** | `src/obituary_enricher.py`, `src/buyer_prospector.py` | Soft fallback — DDGS used as backup. | Per-search |
| **Firecrawl** | `src/obituary_enricher.py` (`FIRECRAWL_BUDGET=3000` default) | Soft — has explicit credit-exhaustion handling (`_firecrawl_credits_exhausted` at line 989). | Per-page |
| **DataSift.ai** | `src/datasift_uploader.py` | Hard for upload step; pipeline produces CSV regardless. UI changes break Playwright selectors. | $97/mo unlimited |
| **Dropbox API** | `src/dropbox_watcher.py`, `src/dropbox_uploader.py` | Hard for photo-import flow only. | Free tier |
| **Google Drive** | `src/drive_uploader.py` | Soft — CSV stays in Apify KVS if upload fails. | Free tier |
| **Ancestry.com** | `src/ancestry_enricher.py` | Soft — Playwright login flow, deep-prospecting only. SSDI search has incomplete `TODO` at line 920 ("family tree parsing is Phase 2"). | Subscription |
| **Madison AssuranceWeb tax roll** | `src/madison_property_api.py`, `src/madison_tax_delinquent_api.py` | Hard for AL probate property-locator + tax-distress. No alternate source. | Free public API |
| **Jefferson E-Ring (capturecama.com)** | `src/jefferson_property_api.py` | Hard. **Plus SSL cert chain incomplete — `verify=False` at line 202** (documented, intentional, but a real MITM exposure window). | Free public API |
| **Huntsville monthly PDF** | `src/huntsville_unsafe_buildings_api.py` | Hard. Auto-discovery walks back 6 months, so a 1-month gap is tolerated. WAF blocks `httpx`; uses `requests` instead. | Free |
| **Birmingham Accela (Playwright)** | `src/birmingham_code_enforcement_api.py` | Hard. ASP.NET WebForms + `__VIEWSTATE` postbacks + Beamer NPS popup blockers. | Free |

**Concentration risk:** the daily pipeline (`python src/main.py daily`) exercises 4-7 of these on every run depending on enabled flags. Apify dashboards have no per-service health metric — failures show up as "fewer records produced today" 24-48h later.

**Recommendation:** add a per-service success-rate metric to the Slack daily report (already partial in `src/slack_notifier.py`). When 2Captcha goes from 99% → 80% solve rate, that's the warning before a failed run.

---

## SSL `verify=False` (security)

`src/jefferson_property_api.py:202` — `verify=False` because `jeffersonexpress.capturecama.com` serves an incomplete SSL chain that Python's `certifi` doesn't resolve, but browser/OS keychains do.

`src/jefferson_tax_delinquent_api.py:234` — same pattern for `jccal.org`.

Both are documented in code comments. The pragmatic risk is low (these are public read-only tax APIs queried from server infrastructure with no PII in the request body), but:
- **Lint/CI complaints:** any future security scanner (bandit, safety) will flag both.
- **Better alternative:** ship the missing intermediate cert as a bundled CA file and pass `verify="path/to/bundle.pem"`. Two days of work; permanently removes the lint warning.
- **Impact today:** mostly aesthetic; the comment block already explains the tradeoff. But it's a precedent — adding a third county adapter from a similar host could end up with `verify=False` copy-pasted as the obvious answer.

---

## Apify-Specific Concerns

### Code-path divergence: `actor_main()` vs `cli_main()`

`src/main.py` has TWO complete main entry points:
- `actor_main()` (lines 126-606) — Apify Actor SDK, async, uses `Actor.push_data()` / KVS / Actor proxy
- `cli_main()` (lines 1380-1900) — argparse, asyncio.run, writes local files

These share `_filter_searches()`, `_preflight_check()`, and the underlying scraper/enrichment pipelines, but the Actor branch has **its own copy** of the post-scrape pipeline (probate property lookup, enrichment, Tracerfy, PDF generation, DataSift upload, Slack notification). Each addition (e.g. the AL probate-property-locator wiring) has to be applied twice and the two branches drift.

**Evidence of drift today:**
- `actor_main()` at line 184 still has the dead `TNPN_EMAIL` validator (the just-found cold-start crash).
- `actor_main()` `_cred_map` doesn't include any APNA credentials at all (lines 147-160) — the Actor input schema would need to be updated and re-pointed.
- The CLI path correctly updated to "alabamapublicnotices.com" but the Actor path has nothing equivalent because the AL site needs no login.

**Fix:** factor a `run_full_pipeline(notices, opts, on_event)` helper that both entry points call. The Actor path supplies an `on_event` that pushes to KVS/dataset; the CLI supplies one that writes files.

### State files are single-machine

| File | Purpose | Multi-instance hazard |
|---|---|---|
| `last_run.json` | last successful daily-run date | Two instances will both believe they ran "today" → next run drops 1 day of overlap |
| `seen_ids.json` | cross-run dedup keyed by notice ID | Two instances each maintain their own seen-set; one instance will reprocess notices the other already pushed |
| `captcha_failed_ids.json` | retry queue for CAPTCHA-failed notices | Two instances will both retry the same failed notice; budget burned twice |
| `cookies.json` | TN session cookies (legacy, AL doesn't need login) | Stale — should be removed since AL is loginless |
| `dropbox_state.json` / `photo_state.json` | Dropbox cursor + per-photo dedup | Two `dropbox-watch` instances will both delete the same Dropbox file |
| `datasift_cookies.json` | DataSift Playwright session | Two upload jobs racing — UI state corruption |

The Apify branch handles `last_run` and `seen_ids` through Apify KVS (`actor_main()` lines 296-306), so those are safe in production. But the **CLI** path at `src/scraper.py:454-475` still writes to local JSON. Anyone running `python src/main.py daily` on a laptop in parallel with the Apify schedule will silently desync.

**Fix:** standardize on Apify KVS for production and document that local CLI is single-developer only. Or: replace local JSON state with a small SQLite file and add an advisory lock.

### Tesseract is not in the Dockerfile

`Dockerfile` (16 lines) installs Python deps from `requirements.txt` and copies source. `requirements.txt` includes `pytesseract>=0.3.10`, but **Tesseract itself is a system binary**, not a pip package. The Apify base image `apify/actor-python-playwright:3.12` does not ship Tesseract. Therefore:

- `python src/main.py photo-import` on Apify will fail at the first `pytesseract.image_to_string` call.
- `python src/main.py pdf-import` (PDF OCR fallback path) will fail similarly.
- The web-scrape daily pipeline does not invoke Tesseract — so the production daily runs are safe.

**Fix:** add a `RUN apt-get update && apt-get install -y tesseract-ocr` line to the Dockerfile. Even if photo-import isn't run on Apify today, the gap is a future-trap.

---

## Architectural Concerns

### Cross-county adapter duplication, no shared base

Four pairs of county adapters (and growing):

| Concept | Jefferson | Madison |
|---|---|---|
| Property search | `jefferson_property_api.py` (370 lines) | `madison_property_api.py` (357) |
| Tax delinquent | `jefferson_tax_delinquent_api.py` | `madison_tax_delinquent_api.py` |
| Code violation | `birmingham_code_enforcement_api.py` (582) | `huntsville_unsafe_buildings_api.py` |

Each pair shares the same conceptual API (search by name, search by address, fetch delinquent list, convert-to-NoticeData) but has **different signatures, different data classes, and different implementation styles** (httpx vs requests vs Playwright). The integration glue in `probate_property_locator.py` and `code_violation_pipeline.py` papers over the differences with `if county == "jefferson"` branches.

**Evidence this is causing bugs today:**
1. The Madison probate-locator name-format bug (above) — Jefferson has retry logic, Madison doesn't. A shared base class with a `normalize_query_name()` extension point would have caught this.
2. Property-record dataclasses diverge: `JeffersonPropertyRecord` has `is_homestead`, `total_value`, `improvement_value`, `municipality`; `MadisonPropertyRecord` has only `is_buildable` (a proxy for homestead). The locator (`_record_to_match` at line 175) uses `getattr(rec, "...", default)` to handle this, which is fragile.
3. The DataSift formatter's per-county tag generation (`municipality_*`) silently produces no tag for Madison records because `MadisonPropertyRecord.municipality = ""` always. CLAUDE.md acknowledges this gap.
4. Adding a third county (Shelby, Lee, etc.) means writing two new adapters with the existing inconsistent shapes — and updating both `_search_jefferson`/`_search_madison`-style dispatchers.

**Fix path:** define a `CountyPropertyAdapter` Protocol in `probate_property_locator.py` that all county adapters conform to. Move name-normalization logic into the protocol-implementing class. Add a registry (`_ADAPTERS = {"jefferson": JeffersonAdapter(), ...}`) so dispatch is data-driven, not branch-driven.

### Three different name-splitters

| Location | Name | Behavior |
|---|---|---|
| `src/data_formatter.py:147` | `_split_name` | "John A. Doe" → ("John", "A. Doe") |
| `src/datasift_formatter.py:154-219` | `_clean_and_split_name` / `_split_name` | "John A. Doe" → ("John", "Doe") (strips middle initial); handles joint names |
| `src/notice_parser.py:1405` | `_split_full_name` | Returns 4-tuple (first, middle, last, suffix); handles "James F. Smith Jr." |
| `src/tracerfy_skip_tracer.py:108` | `_split_name` | Yet a fourth implementation |

The same `owner_name` string flowing into the CSV exporter and the DataSift formatter produces **different first/last splits** depending on which path is taken. This causes downstream record mismatches when DataSift dedupes by `firstname + lastname`.

**Fix:** consolidate on `notice_parser._split_full_name` (the most complete) and have the other three call it with appropriate field selection.

### `datasift_uploader.py` is 4,246 lines

Single file containing: login, upload wizard (5 steps), enrich, skip-trace, preset management, sequence builder, SiftMap sold-property workflow. **186 `wait_for_timeout` calls** (largely 1-3s pauses for SPA settling). Any UI change at DataSift requires triaging across this monolith.

**Fix path:** split by workflow into separate modules (`datasift_login.py`, `datasift_upload.py`, `datasift_presets.py`, `datasift_sequences.py`, `datasift_siftmap.py`). The shared session/login layer is small (~200 lines). Could be done incrementally.

### `main.py` is 1,905 lines with 51 imports (many lazy/local)

Mode dispatch is a long if-chain (`if args.mode == "X": ...`). 13+ modes registered. A new mode adds an arg to argparse, an if-branch, and a function. Each of those requires touching `main.py`. The import-at-call-site pattern (e.g. `from deal_analyzer import run_deal_analysis` inside the if-block at line 1506) is consistent, but it makes static analysis (autocomplete, type checking) painful and means import errors only surface when the mode is actually used.

**Fix path:** mode registry pattern — each mode is a class/dataclass with `add_args(parser)` and `run(args)` methods. `main.py` discovers them and dispatches.

---

## Test Suite Inconsistency

**Tests at repo root:**
- `test_ancestry.py`, `test_datasift_upload.py`, `test_e2e_record.py`, `test_e2e_smyth.py`, `test_entity_upload.py`, `test_existing_list_upload.py`, `test_manage_presets.py`, `test_manage_sold.py`, `test_phone_validator.py`, `test_tracerfy_discovery.py`, `test_tracerfy_upload.py` — **11 scripts**

**Tests under `tests/`:**
- `tests/test_captcha_live.py`, `tests/test_deceased_detection.py`, `tests/test_e2e_obituary.py`, `tests/test_entity_researcher.py`, `tests/test_obituary_enricher.py`, `tests/test_parser.py`, `tests/test_parser_edge_cases.py`, `tests/test_pdf_importer.py` — **8 files**
- Plus utility scripts: `export_review_xlsx.py`, `export_template_xlsx.py`, `merge_foreclosure.py`, `reenrich.py`, `run_dm_address_backfill.py`, `run_obituary_enrichment.py`
- Plus fixture: `tests/obituary_ground_truth.json`

The repo-root tests are mostly **integration/E2E scripts** that require live credentials and write to real services. The `tests/` subdir is closer to unit tests + fixture-driven validation, but is also mixed with one-off ETL utilities (`merge_foreclosure.py`, `reenrich.py`, `run_dm_address_backfill.py`).

**Concrete problems:**
1. No CI: there's no `.github/workflows/`, no pytest fixtures, no `conftest.py`. `pytest tests/` may or may not work — depends on the test author's import style.
2. The repo-root tests can't be discovered by `pytest tests/` and aren't gated.
3. `tests/test_captcha_live.py` will burn 2Captcha credits if run by accident.
4. There's no separation between "live tests requiring credentials" (need quarantine) and "unit tests" (always-on).
5. None of the new AL pipeline modules (`probate_property_locator.py`, `madison_property_api.py`, `jefferson_property_api.py`, `huntsville_unsafe_buildings_api.py`, `birmingham_code_enforcement_api.py`) have any tests. The Madison name-format bug above would have been caught by a single golden-test fixture.

**Fix path:**
1. Move all repo-root `test_*.py` into `tests/integration/` and add a `requires_credentials` pytest marker.
2. Create `tests/unit/` for fast offline tests (parser regex, name splitters, property-locator scoring).
3. Move `merge_foreclosure.py`, `reenrich.py`, etc. out of `tests/` into `scripts/` or `tools/`.
4. Add a `conftest.py` with a `--run-live` flag that enables credentialed tests.

---

## Tech Debt

### Floating dependency versions (no upper bounds)

`requirements.txt` uses `>=` on every package. `playwright>=1.40.0`, `apify>=2.0.0`, `anthropic>=0.40.0`, `dropbox>=12.0.2`, etc. No lockfile (no `requirements.lock`, no `poetry.lock`, no `uv.lock`).

**Risk profile:**
- `apify>=2.0.0` — Apify SDK had breaking changes in 2.x → 3.x (currently latest).
- `dropbox>=12.0.2` — comment in CLAUDE.md says "minimum for post-Jan-2026 API compatibility" — explicit migration window.
- `anthropic>=0.40.0` — current is 0.7x; SDK has had multiple breaking changes around streaming and message formats.
- `playwright>=1.40.0` — selectors and timeouts behavior changes between minor versions.
- `ddgs>=9.0.0` — unstable (fork of duckduckgo-search), high churn.

`pip install -r requirements.txt` today produces a different environment than 30 days from now. Production reproducibility is whatever `pip` resolves on the day of build.

**Fix:** generate `requirements.lock` via `pip-compile` (pip-tools) or migrate to `uv` / `poetry`. Pin upper bounds (e.g. `playwright>=1.40,<2.0`).

### Each enricher redefines `REQUEST_DELAY_MIN/MAX`

`src/comp_analyzer.py:34-35`, `src/property_enricher.py:24-25`, `src/obituary_enricher.py:1406-1407` each redefine these constants, shadowing `config.REQUEST_DELAY_MIN/MAX` (2.0/3.0). The local copies are 1.0/2.0 — **half the global setting**. Either the global is too conservative or the locals are too aggressive; nobody can tell from a code reading.

**Fix:** delete the local copies, import from `config`. Tune the central constant if rate-limiting causes problems.

### Hardcoded tax year `2025`

| File | Line | Default |
|---|---|---|
| `src/jefferson_property_api.py` | 255, 293 | `year: int = 2025` |
| `src/madison_property_api.py` | 135, 183, 256 | `year: int = 2025` |
| `src/jefferson_tax_delinquent_api.py` | 52 | `DEFAULT_TAX_YEAR = 2024` |

Today is 2026-04-30; the May 2026 auction uses the 2025 delinquent list (per AL § 40-10-15 — "the prior year's tax roll"). `DEFAULT_TAX_YEAR=2024` in `jefferson_tax_delinquent_api.py` is a year stale and will break silently when a caller forgets to pass `year=2025`.

**Fix:** compute the default tax year as `current_year - 1` (or `current_year - 1 if before May 5 else current_year`).

### TODO / debug-script residue

- `src/ancestry_enricher.py:920` — `# TODO: Parse family tree results — need to discover selectors`. Family tree search is a stub returning `[]`. Functionally fine (the SSDI search path works), but listed as "Phase 2" with no scheduled phase.
- The git status header on this run referenced `src/debug_notice_page.py` and `src/debug_snippets.py`; both files have since been removed (probably squashed before commit). No action needed — the gitStatus snapshot was stale at the start of the session.

### Broad exception catches concentrated in 4 files

```
datasift_uploader.py — 39 except blocks
main.py              — 21
obituary_enricher.py — 19
ancestry_enricher.py — 12
```

Most are `except Exception: pass` swallows around Playwright operations (UI-change tolerance). They make debugging UI breakage hard — a selector change becomes a silent zero-records run instead of a clear stack trace. Logging the exception (even at debug level) before swallowing would help; current pattern often just `pass`es.

**Fix:** convert blanket `except Exception: pass` into `except Exception as e: logger.debug("Optional step failed (%s): %s", step_name, e)` — a one-liner change that preserves behavior and adds observability.

### Mortality of the "courthouse photo pipeline"

CLAUDE.md "Courthouse Photo Pipeline (build 1.0.28+)" describes Knox/Blount courthouse terminal photos → Dropbox → OCR → CSV. With the AL migration (Jefferson + Madison), courthouse photos are no longer the primary source — the AL counties expose web-scrapable APIs and PDFs. The photo-import code path (`photo_importer.py`, `dropbox_watcher.py`, `image_utils.py`) is still wired and tested for TN courthouses but has no AL integration. Either:
- (a) deprecate the photo path entirely (and remove its CLI mode + dropbox watcher), or
- (b) extend the LLM prompts in `llm_parser.py` to handle AL-format photos (which are different in layout from TN courthouse terminals).

Without a decision, this is dead-but-loaded code that gets touched in every config refactor.

---

## Performance / Scaling Concerns

### CAPTCHA latency dominates throughput

Per-notice timing budget:
- Result page paginate: ~3-5s
- Detail page navigate: ~2s
- **2Captcha solve: ~10-30s**
- Notice text parse + LLM fallback: ~2-5s
- **Total per notice: ~17-42s**

A 50-notice page → ~15-35 minutes of wall time. The daily run for 14 saved searches × 7-day window can easily push 30-60 minutes. There is no parallelism: notices are processed sequentially on a single Playwright page.

**Optimization paths:**
1. Use 2Captcha's "callback" mode instead of polling (saves 5-10s per notice average).
2. Solve N captchas in parallel using N pages on the same browser context.
3. Pre-filter snippet text (already collected pre-CAPTCHA) more aggressively to skip CAPTCHA on notices that are clearly not target-county.

### `seen_ids.json` grows unbounded

Pruned only when older than `SEEN_IDS_PRUNE_DAYS = 90` (`src/scraper.py:467`). At the rate of 50-200 new IDs/day across 14 searches, the file caps around 18,000 IDs — under 1MB JSON. Not currently a problem, but at production scale (multiple counties), worth converting to SQLite or just timestamping each entry.

### `datasift_uploader.py` upload wizard timeouts

CLAUDE.md notes "Increase DataSift upload wizard timeouts for headless cloud environments" (commit 459c562). Current pattern: `await page.wait_for_timeout(8000)` after key transitions. These timings are tuned to a specific DataSift UI version; any DataSift redesign will require sweeping all 186 timeout values.

---

## Test Coverage Gaps

| Untested area | Files | Risk |
|---|---|---|
| AL probate property locator | `src/probate_property_locator.py`, `madison_property_api.py`, `jefferson_property_api.py` | **High** — Madison name-format bug above would have been caught. |
| AL probate metadata extraction | `src/notice_parser.py:1340-1700` (the +730 line diff) | **High** — case_number, judge_name, granted_date, creditor_deadline, hearing_date all new and untested. |
| Vacant-land + validator probate exemption | `src/enrichment_pipeline.py:99-285` | Medium — the exemption set `_NO_PROPERTY_ADDRESS_TYPES` was added today; no fixture validates that probate notices without address-but-with-PR-mailing pass. |
| AL foreclosure title variants | `src/foreclosure_filter.py:34-50` | Medium — 6 new include phrases. |
| Birmingham Accela scraper | `src/birmingham_code_enforcement_api.py` (582 lines) | Medium — Playwright + ASP.NET ViewState; UI changes break silently. |
| Apify Actor cold-start path | `src/main.py` `actor_main()` | **High** — the TNPN_EMAIL bug above would have been caught by a 5-line unit test. |
| Name-splitter consistency | `data_formatter._split_name` vs `datasift_formatter._split_name` | Low — observable mismatch in DataSift dedup. |
| State-file recovery (corrupt JSON) | `config.load_state()` at `src/config.py:333` | Low — has `.bak` fallback but no test that actually corrupts and recovers. |

---

*Concerns audit: 2026-04-30*
