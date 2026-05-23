# Testing Patterns

**Analysis Date:** 2026-04-30

## Test Framework

**Runner:**
- **None.** No `pytest`, no `unittest`, no `nose`. Tests are plain Python scripts that use bare `assert` statements and exit non-zero on failure.
- No test framework appears in `requirements.txt`.
- No `pyproject.toml`, `setup.cfg`, `pytest.ini`, `tox.ini`, or `conftest.py` exists in the repo.

**Assertion style:**
- Plain `assert` for unit-style tests in `tests/` — fails with `AssertionError` if the check fails
- Manual `if … else: print("FAIL")` for tests that need custom messages or accumulate multiple failures (see `tests/test_parser_edge_cases.py:27-46` `check()` helper)
- Live integration tests (`test_*.py` at repo root) print results to stdout in human-readable form rather than asserting; the human watching the headed browser is the assertion oracle

**Mocking:**
- **None.** No `unittest.mock`, no `pytest-mock`, no `responses` library. `grep -rn "Mock|patch|monkeypatch|fixture" tests/ test_*.py` returns nothing.
- The codebase makes a deliberate trade: tests hit live APIs (real money cost) rather than maintain mock fixtures that drift from upstream reality.

**Run commands:**
```bash
# Unit-style tests in tests/ (offline-runnable)
python tests/test_parser.py
python tests/test_parser_edge_cases.py
python tests/test_pdf_importer.py
python tests/test_obituary_enricher.py     # offline (no API calls)
python tests/test_deceased_detection.py
python tests/test_entity_researcher.py

# Live integration tests in repo root (headed browsers, real APIs, real $)
python test_e2e_record.py                  # full pipeline on one record (~$0.15)
python test_e2e_smyth.py                   # full pipeline on Smyth case
python test_datasift_upload.py             # headed Playwright DataSift upload
python test_manage_presets.py --all        # headed DataSift preset management
python test_manage_sold.py                 # headed SiftMap sold workflow
python test_phone_validator.py --csv-path PATH --no-upload
python test_ancestry.py --search "John Smith" --explore
python test_tracerfy_discovery.py          # API field discovery
python test_tracerfy_upload.py             # Tracerfy + DataSift integration
python test_entity_upload.py               # Entity enrichment + DataSift upload
python test_existing_list_upload.py        # Repair upload to existing list

# Live tests under tests/ (require network)
python tests/test_captcha_live.py          # full scrape + 2Captcha solve
python tests/test_e2e_obituary.py          # obituary enrichment with real LLM call
```

There is no single "run all tests" command. Each test is invoked individually.

## Test File Organization

The repo has **two test locations** with distinct purposes:

**`tests/` directory (offline unit-style tests):**
| File | Purpose |
|---|---|
| `tests/test_parser.py` | `notice_parser.py` regex extraction against captured real notice text |
| `tests/test_parser_edge_cases.py` | Address + name extraction against ~40 hand-crafted edge cases |
| `tests/test_pdf_importer.py` | OCR-row regex (`ROW_RE`, `PARCEL_RE`) for tax-sale PDF ingestion |
| `tests/test_obituary_enricher.py` | Name parsing + decision-maker ranking (no API calls) |
| `tests/test_deceased_detection.py` | `detect_deceased_indicator()` from `tax_enricher.py` (string heuristic) |
| `tests/test_entity_researcher.py` | Entity-name parsing logic |
| `tests/test_e2e_obituary.py` | Live obituary search + Claude Haiku — requires `ANTHROPIC_API_KEY` |
| `tests/test_captcha_live.py` | Full live scrape — requires login + 2Captcha credit |

`tests/README.md` (3 lines) documents only that `obituary_ground_truth.json` is sourced from public records.

**Repo-root `test_*.py` files (live integration / headed browser tests):**

These are NOT discoverable as a "test suite" in the pytest sense — they're CLI tools shaped like tests, used during feature development to drive headed Playwright browsers and watch them execute against real DataSift / DataSift / Ancestry / Tracerfy / Trestle endpoints.

| File | What it drives | Running cost |
|---|---|---|
| `test_e2e_record.py` | Full enrichment pipeline (obituary → Tracerfy → Trestle → PDF) on Daniel H. Williams record | ~$0.15 (Tracerfy + Trestle + Haiku) |
| `test_e2e_smyth.py` | Full pipeline + report generation on the Smyth case | ~$0.15 |
| `test_datasift_upload.py` | Headed DataSift upload + Enrich + Skip Trace | DataSift quota only |
| `test_manage_presets.py` | Headed DataSift preset discovery / sold-exclusion update / sequence creation | DataSift quota only |
| `test_manage_sold.py` | Headed SiftMap sold-property tagging | DataSift quota only |
| `test_phone_validator.py` | DataSift phone-export → Trestle validation → DataSift tag-upload (full chain, headed) | ~$0.015/phone (Trestle) |
| `test_ancestry.py` | Headed Ancestry.com login + SSDI / Newspapers.com search | Ancestry sub. cost |
| `test_tracerfy_discovery.py` | Tracerfy API recon — discovers response field names on instant + batch endpoints | ~$0.06/record |
| `test_tracerfy_upload.py` | Tracerfy batch + DataSift CSV column-mapping verification | DataSift quota + ~$0.06 |
| `test_entity_upload.py` | Entity-research → DataSift upload column verification | DataSift quota only |
| `test_existing_list_upload.py` | Repair-CSV upload into an existing DataSift list | DataSift quota only |

## Test Structure Conventions

**`sys.path.insert` is the universal idiom** — all test files prepend the `src/` directory to `sys.path` so they can import project modules:

```python
# tests/test_parser.py:6-8
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from notice_parser import NoticeData, _parse_address, _parse_name

# test_e2e_record.py:13-15 — slightly different but same intent
src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
sys.path.insert(0, src_dir)
os.chdir(src_dir)

# test_phone_validator.py:26
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
```

Tests rely on `.env` being loaded — most live tests do `from dotenv import load_dotenv; load_dotenv()` early. Module-level code reads `os.environ` or `cfg.<NAME>` after that.

**Three test runner patterns coexist:**

1. **Function-discovery runner** (preferred for offline tests):
```python
# tests/test_obituary_enricher.py:562-578
if __name__ == "__main__":
    passed = 0
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                passed += 1
                print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
```

2. **Class-based runner** (used in `tests/test_pdf_importer.py:185-219`):
```python
test_classes = [TestRowRegex, TestParcelRegex, TestParsePageRegex, TestValidateRow]
for cls in test_classes:
    instance = cls()
    methods = [m for m in dir(instance) if m.startswith("test_")]
    for method_name in sorted(methods):
        ...
```
The classes are *not* `unittest.TestCase` subclasses — they're plain `class TestX:` containers used purely for organization.

3. **Imperative top-level script** (used in many `test_*.py` at repo root):
The whole test is a flat sequence of `print` + `assert` statements at module top level, executed by `python test_e2e_record.py`. No `if __name__ == "__main__":` wrapper, no functions. See `test_e2e_record.py` lines 26-262 — the entire test runs at import time.

## Mocking Posture

**Live integration over mocks — by design.**

The codebase has no mocks anywhere. Every test that exercises a network-bound code path makes the real HTTP / Playwright call. This is a deliberate trade:

**Why this works for SiftStack:**
- External services (Smarty, Zillow via OpenWebNinja, Knox Tax API, Tracerfy, Trestle, DataSift, Ancestry, alabamapublicnotices.com, Madison/Jefferson tax portals) all change their response shape silently. A mock that matches today's contract drifts the moment the upstream service tweaks a field name.
- The `test_tracerfy_discovery.py` file is essentially a confession: when a new API needs to be integrated, the first "test" is a real-API recon script that prints `sorted(p.keys())` so the developer can see the actual field names.
- The codebase's "log + continue" error handling (see CONVENTIONS.md) means production code is already resilient to upstream weirdness — verifying that resilience requires real upstream weirdness, which mocks can't reproduce.

**What gets tested without mocks:**
- Pure-function logic (regexes, name parsing, scoring, classification): `tests/test_parser.py`, `tests/test_parser_edge_cases.py`, `tests/test_pdf_importer.py`, `tests/test_obituary_enricher.py`, `tests/test_deceased_detection.py`. These run offline because the unit under test is pure.
- Anything that touches a network is tested live in repo-root `test_*.py` scripts.

**Cost discipline:**
Live tests print expected and actual cost (e.g. `test_e2e_record.py` lines 224-233 prints `Tracerfy: $0.06 / Trestle: $0.08 / TOTAL: ~$0.15`). Tests that hit paid APIs check for the API key first and skip cleanly:
```python
# test_e2e_record.py:67-71
if not cfg.TRACERFY_API_KEY:
    print("  SKIP — TRACERFY_API_KEY not set")
    tracerfy_stats = {"submitted": 0, "matched": 0, ...}
else:
    tracerfy_stats = batch_skip_trace([notice], max_signing_traces=5)
```

## Headed Playwright Test Pattern

DataSift / Ancestry / SiftMap have no public APIs. All UI workflows are tested in headed mode (`headless=False`) so the developer can watch the browser execute and visually verify each step.

**Canonical structure (see `test_datasift_upload.py:94-163`):**
```python
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=False)  # HEADED for testing
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
    )
    page = await context.new_page()

    # 1. Login
    logged_in = await login(page, email, password)
    if not logged_in:
        logger.error("Login failed!"); await browser.close(); return

    # 2. Drive the workflow under test
    result = await upload_csv(page, csv_path, list_name=list_name)

    # 3. Pause for human inspection BEFORE the next step
    logger.info("Pausing 10s before enrichment so you can inspect...")
    await page.wait_for_timeout(10000)
    enrich_result = await enrich_records(page, list_name)

    # 4. Hold the browser open at the end
    logger.info("Browser will stay open for 30 seconds for inspection...")
    await page.wait_for_timeout(30000)
    await browser.close()
```

Key elements:
- **Always `headless=False`** in test files. Production runs (`src/main.py --upload-datasift`) use `headless=True`; the test variant pins it to `False`.
- **Pauses between steps** (`await page.wait_for_timeout(10000)` — 10 seconds) so the human can read the screen between transitions.
- **Final inspection window** (30-60 seconds at the end) so the result is visible after the workflow completes.
- **Screenshots on key events** — `await page.screenshot(path="datasift_after_login.png")` for post-mortem debugging when the test runs unattended.

The Playwright workflow under test (`upload_csv`, `enrich_records`, `skip_trace_records`, `run_manage_presets_workflow`, `run_manage_sold_workflow`) is itself the production code from `src/datasift_uploader.py` — there is no test-only fork. The test just exercises it under a different launch flag.

## What Is NOT Tested

This is significant: very large parts of the codebase have no automated tests at all.

**Untested modules (no test file references them):**
- `src/scraper.py` — the core APN scraping logic (Playwright + 2Captcha + ASP.NET ViewState handling). Only `tests/test_captcha_live.py` exercises a thin slice end-to-end.
- `src/foreclosure_filter.py` — the include/exclude phrase classifier. No tests for `is_valid_foreclosure()`.
- `src/data_formatter.py` — CSV writer and dedup logic. Verified only ad-hoc via running `python src/main.py daily`.
- `src/datasift_formatter.py` — 41-column DataSift CSV builder. Only verified end-to-end via `test_datasift_upload.py` (which is a headed browser test, not a unit test of the formatter).
- `src/enrichment_pipeline.py` — the 10+ step canonical pipeline. No tests.
- `src/madison_property_api.py`, `src/jefferson_property_api.py` — county property lookup adapters. Both have CLI entry points (`_main`) used as ad-hoc smoke tests, but no automated tests.
- `src/madison_tax_delinquent_api.py`, `src/jefferson_tax_delinquent_api.py`, `src/huntsville_unsafe_buildings_api.py`, `src/birmingham_code_enforcement_api.py` — all county adapters added in 2026-04. Same posture: CLI smoke tests, no automated tests.
- `src/probate_property_locator.py` — multi-tier name-search orchestrator. CLI smoke test, no automated tests.
- `src/captcha_solver.py`, `src/llm_parser.py`, `src/llm_client.py`, `src/address_standardizer.py`, `src/property_enricher.py`, `src/tax_enricher.py` — verified live but not unit-tested.
- `src/photo_importer.py`, `src/dropbox_watcher.py`, `src/pdf_importer.py` (parser layer is tested in `tests/test_pdf_importer.py`; the OpenCV pipeline + Tesseract calls are not).
- `src/comp_analyzer.py`, `src/rehab_estimator.py`, `src/deal_analyzer.py` — deal-analysis modules with hardcoded financial constants. No tests verify the math.
- `src/market_analyzer.py`, `src/extract_market_finder.py`, `src/buyer_prospector.py` — market-intelligence modules. No tests.
- `src/datasift_uploader.py`, `src/datasift_core.py` — only verified via headed `test_*.py` scripts.
- `src/slack_notifier.py`, `src/drive_uploader.py`, `src/dropbox_uploader.py`, `src/excel_exporter.py`, `src/report_generator.py`, `src/sequence_templates.py`, `src/playbook_generator.py` — no tests.

**What IS tested (offline):**
- Regex parsers (`tests/test_parser.py`, `tests/test_parser_edge_cases.py`, `tests/test_pdf_importer.py`)
- Name-parsing + ranking heuristics (`tests/test_obituary_enricher.py` — 559 lines, the most thorough offline test)
- Deceased-indicator string heuristic (`tests/test_deceased_detection.py`)
- Entity-name parsing (`tests/test_entity_researcher.py`)

**Approximate coverage estimate:** offline unit tests cover ~5-8 modules out of ~54 in `src/`. Of the heavy lifters in the daily pipeline (`scraper`, `enrichment_pipeline`, `data_formatter`, `datasift_formatter`, `datasift_uploader`, all 6 county adapters), zero have automated tests.

The risk this creates: silent breakage when an upstream API or county portal changes shape. Mitigation in production is the "log + continue" error handling — bad records are skipped, the run summary surfaces the count, and the operator manually investigates.

## Manual-Verification Posture for UI Flows

DataSift / SiftMap / Ancestry / Market Finder workflows are verified manually, not automatically. The pattern is:

1. **Develop** in a feature branch with the headed-browser test running locally — watch the Playwright browser execute the new selectors.
2. **Iterate on selectors** when DataSift's styled-components change (which they do — see CLAUDE.md "DataSift UI Automation Patterns" for the catalog of known SPA quirks: pointer interception, Beamer popups, panel scrolling, React DnD with `draggable="false"`, etc.).
3. **Pause + inspect** at each step. Most test scripts have explicit `await page.wait_for_timeout(10000)` between actions specifically so the developer can visually check that the previous step worked.
4. **Hold the browser open at the end** (30-60 seconds, sometimes longer) to verify final state in DataSift.
5. **Screenshot critical events** — `await page.screenshot(path="datasift_after_login.png")` to disk for after-the-fact diagnosis when the test ran headless or unattended.

There is no Selenium-grid / Browserstack / Percy visual-regression layer. Pixel-level UI regression isn't tested.

**Discovery scripts as tests:**
When integrating a new external API, the first "test" is a recon script that calls the real API and prints field names. `test_tracerfy_discovery.py` is the canonical example — it makes a real $0.06 API call to instant-trace endpoint and `print(sorted(p.keys()))`. Production code is then written to match the actually-observed schema.

## Test Data

**Real captured payloads are committed into test files.** The tests don't load fixtures from disk (other than `tests/obituary_ground_truth.json`); instead, real notice text, real OCR output, and real street addresses are embedded directly in the test source.

```python
# tests/test_parser.py:21-78 — full real notice text from notice ID 509975
FULL_PAGE_TEXT = """About Public Notices
|
Help
Welcome, ty@volunteerhomebuyers.com
...
SUCCESSOR TRUSTEE'S NOTICE OF SALE OF REAL ESTATE … DANIEL H. WILLIAMS …
5100 Stokely Ln., Knoxville, Knox County, TN 37918 …"""

# tests/test_parser_edge_cases.py:69-72 — ~40 hand-crafted address strings
addr_test("std-foreclosure",
    "...commonly known as 5100 Stokely Lane, Knoxville, Tennessee 37918. "
    "Sale at 400 Main Street...",
    "5100 Stokely Lane", "Knoxville", "37918")
```

**`tests/obituary_ground_truth.json`** — the only on-disk fixture. Contains test records sourced from publicly-available obituary listings (legacy.com, dignitymemorial.com) and county public-notice filings, used to validate the obituary enrichment pipeline accuracy. Per `tests/README.md`: "All information is derived from public records."

When adding new tests, follow this pattern: paste the real captured upstream output (notice text, OCR result, API JSON snippet) directly into the test file as a string literal, with comments noting the source (e.g. `# captured from notice ID 509975, 2026-02-20`).

## Common Patterns

**Async test entry point** (live tests using Playwright):
```python
async def main():
    ...

if __name__ == "__main__":
    asyncio.run(main())
```
See `test_datasift_upload.py:34-169`, `test_phone_validator.py:61-211`, `test_manage_presets.py:28-123`.

**API-key gating** (skip cleanly when credentials missing):
```python
if not cfg.TRACERFY_API_KEY:
    print("  SKIP — TRACERFY_API_KEY not set")
    tracerfy_stats = {"submitted": 0, "matched": 0, ...}
else:
    tracerfy_stats = batch_skip_trace([notice], max_signing_traces=5)
```

**Headed launch (test) vs headless launch (production):**
The same workflow code in `src/datasift_uploader.py` is invoked with `headless=False` in tests and `headless=True` in `src/main.py --upload-datasift`. Tests pass `headless=False` explicitly; production reads it from CLI flag.

**`argparse` for test parameterization** — most repo-root test files accept CLI args so the developer can re-run specific scenarios without editing code:
```bash
python test_phone_validator.py --csv-path "Phone Enrichment.csv" --no-upload
python test_manage_presets.py --discover
python test_manage_sold.py --counties Knox --months-back 2
```

**Test files are also documentation.** Each repo-root `test_*.py` opens with a docstring listing example invocations — see `test_phone_validator.py:1-18`, `test_manage_presets.py:1-11`, `test_manage_sold.py:1-11`. New tests should follow this convention so the operator can find usage examples without reading the code.

---

*Testing analysis: 2026-04-30*
