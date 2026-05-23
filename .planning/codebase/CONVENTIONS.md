# Coding Conventions

**Analysis Date:** 2026-04-30

SiftStack is a Python 3.12 project that has accumulated a strong, internally-consistent house style. There is no `pyproject.toml`, `.editorconfig`, or `.flake8` — conventions are enforced socially via review, not tooling. Read this document before writing new code; the patterns are tight enough that mismatches are immediately visible.

## Naming Patterns

**Files:**
- Module names are `snake_case.py` (`notice_parser.py`, `madison_property_api.py`, `tax_distress_pipeline.py`)
- Test files at repo root use `test_*.py` (live integration tests)
- Test files under `tests/` use `test_*.py` (offline unit-style tests)
- County-pair adapters share a name root: `madison_property_api.py` + `jefferson_property_api.py`, `madison_tax_delinquent_api.py` + `jefferson_tax_delinquent_api.py`
- Pipeline orchestrators use the `_pipeline.py` suffix: `enrichment_pipeline.py`, `tax_distress_pipeline.py`, `code_violation_pipeline.py`

**Functions:**
- Public API functions: `snake_case` with full verb-noun phrasing (`search_by_owner_name`, `enrich_with_owner`, `fetch_delinquent_parcels`, `apply_auction_dates`)
- Private/internal helpers: leading underscore (`_parse_address`, `_normalize_municipality`, `_post_search`, `_dod_sanity_check`)
- CLI entry points: `_main(argv: list[str]) -> int` invoked via `raise SystemExit(_main(sys.argv[1:]))` at module bottom (see `src/madison_property_api.py:329-357`, `src/jefferson_property_api.py`)
- Async functions are `async def`; this codebase blends sync (HTTP via `httpx`) and async (Playwright) freely — sync helpers are NOT renamed when called from async paths

**Variables:**
- Local variables: `snake_case` (`balance_due`, `notice_id`, `search_terms`)
- Constants at module top: `UPPER_SNAKE_CASE` (`MAX_DOD_GAP_YEARS`, `BASE_URL`, `REQUEST_DELAY_MIN`, `BUSINESS_RE`)
- Module-private constants: leading underscore (`_TOKEN_RE`, `_RECORD_RE`, `_DISP_CODE_DISPLAY`, `_NO_PROPERTY_ADDRESS_TYPES`)
- Compiled regex patterns: `_NAME_RE`, `_ADDR_RE` suffix convention (`_TARGET_COUNTIES`, `_LEADING_DIRECTIONAL_RE`, `_MORTGAGEE_RE`)

**Types/Classes:**
- Dataclasses: `PascalCase` (`NoticeData`, `MadisonPropertyRecord`, `JeffersonPropertyRecord`, `SearchConfig`, `PipelineOptions`, `HuntsvilleUnsafeRecord`, `BirminghamEnforcementRecord`)
- Backwards-compat aliases use `=` assignment, not subclassing: `SavedSearch = SearchConfig` in `src/config.py:124`
- No abstract base classes / Protocol / interfaces — duck typing throughout

## Code Style

**Formatting:**
- No formatter config — code is hand-formatted PEP 8-ish
- Indentation: 4 spaces
- Line length: ~100-120 characters; long string literals (regexes, prompts) routinely exceed
- Trailing comma on multi-line lists/tuples/dicts
- Two blank lines between top-level functions/classes (PEP 8)
- One blank line between methods

**Linting:**
- No `.eslintrc` / `ruff.toml` / `flake8` — no automated linting in CI
- Import ordering follows PEP 8 by convention but is not enforced

**Section banners:**
Hand-rolled box-drawing comment headers organize each module into logical sections. This is the single most distinctive style marker:
```python
# ── Paths ──────────────────────────────────────────────────────────────
# ── Credentials ────────────────────────────────────────────────────────
# ── Site URLs ──────────────────────────────────────────────────────────
```
Use `── Section Name ──` (note the U+2500 BOX DRAWINGS LIGHT HORIZONTAL char, not regular dashes). See `src/config.py`, `src/notice_parser.py`, `src/madison_property_api.py:34, 48, 100, 175, 326`.

## Type Hints

**Coverage:** Type hints are used pervasively but not exhaustively — they appear on:
- All public function signatures (parameters AND return types)
- Module-level constants when the type is non-obvious (`TN_CITIES: list[str]`, `_KNOWN_CITIES_SET: set[str]`, `SAVED_SEARCHES: list[SearchConfig]`)
- Local variables when ambiguous (`failures: list[str] = []`, `by_type: dict[str, int] = {}`)

**Style:**
- PEP 604 union syntax: `list[str] | None`, `str | None` (not `Optional[str]` or `Union[...]`)
- Modern generic syntax: `list[str]`, `dict[str, int]`, `tuple[str, str, str, str]` (not `List[...]`)
- `from __future__ import annotations` used in 11 files (e.g. `src/madison_property_api.py:18`, `src/jefferson_property_api.py:18`) — primarily county adapter modules to allow forward-reference of dataclass types in `from_*_record` classmethods
- `typing.Iterator` is imported when needed (`src/madison_property_api.py:25`); other typing names rarely imported

**No mypy or other static type checker is configured** — types are documentation, not enforcement.

## Dataclass Patterns

Dataclasses are the default container for any structured data. Three distinct flavors are used:

**Mutable enrichment dataclass — the `NoticeData` pattern:**
- Defined in `src/notice_parser.py:28-169`
- Plain `@dataclass` (mutable, comparable)
- All fields default to `""` (string) — never `None`, never `Optional`. This is intentional: empty strings flow through CSV writers, regex `.search()` calls, and string formatting without `None` checks.
- Field count grows aggressively over time (~140 fields as of 2026-04). Every new enrichment step adds fields rather than nesting sub-objects.
- Fields are grouped by enrichment stage with section comments:
  ```python
  # Smarty address standardization fields (populated post-scrape)
  # Zillow property enrichment fields (populated post-scrape)
  # Probate-specific fields
  # Multi-parcel + homestead enrichment (Tweak #1)
  ```
- New code adding fields to `NoticeData` should append to an existing section or open a new one — DO NOT reorganize existing fields (downstream code relies on attribute presence, not order).

**Frozen value-record dataclass — the `_PropertyRecord` pattern:**
- Used by all county adapters: `MadisonPropertyRecord`, `JeffersonPropertyRecord`, `MadisonDelinquentRecord`, `JeffersonDelinquentRecord`, `HuntsvilleUnsafeRecord`, `BirminghamEnforcementRecord`
- `@dataclass(frozen=True)` — immutable, hashable
- Real types (`float`, `int`, `bool`) — NOT all-strings like `NoticeData`. Conversion from raw API payloads happens in the dataclass.
- Constructor pattern: `@classmethod from_kendo_record(cls, raw: dict) -> "MadisonPropertyRecord"` or `from_api_record(cls, raw: dict)` — encapsulates all field coercion (`float(raw.get("BalanceDue") or 0)`, ` " ".join(raw.get("MigratedOwners").split())`, etc.). See `src/madison_property_api.py:69-97`, `src/jefferson_property_api.py:134-187`.
- Derived fields are computed inside the classmethod (e.g. `is_buildable`, `is_homestead`, `is_delinquent`, `is_high_exposure`)
- Cross-county field parity is preserved by stub fields. E.g. `MadisonPropertyRecord.municipality = ""` exists only because `JeffersonPropertyRecord.municipality` is meaningful — the docstring explicitly notes "Kept for cross-county field parity" (`src/madison_property_api.py:67, 96`).

**Config dataclass — the `SearchConfig` pattern:**
- Plain `@dataclass`, mutable (no `frozen=True`)
- Used to declare static configuration data parsed at module load: `SearchConfig` in `src/config.py:107-121`, `PipelineOptions` in `src/enrichment_pipeline.py:30-60`
- Optional fields go at the end with default values (`notice_subtype: str = ""` in `SearchConfig`)

## Error Handling

**The "log + continue" idiom (the dominant pattern):**

The codebase has a strong, deliberate idiom for enrichment-pipeline error handling: catch broad `Exception`, log via `logger.warning` or `logger.exception` with context, and continue. One bad record never kills a batch.

```python
# src/property_lookup.py:179-180 — KGIS lookup wraps the whole adapter
try:
    ... # entire Playwright session
except Exception as e:
    logger.warning("KGIS lookup failed for '%s': %s", name, e)

# src/scraper.py:296-301 — per-notice retry loop swallows everything
try:
    ... # parse one notice
    return notice
except PwTimeout:
    logger.warning("  Timeout on notice %s (attempt %d/%d)", notice_id, attempt, MAX_RETRIES)
    await delay()
except Exception:
    logger.exception("  Error on notice %s (attempt %d/%d)", notice_id, attempt, MAX_RETRIES)
    await delay()
```

Why: every enrichment step (Smarty / Zillow / Knox Tax API / obituary search / Tracerfy / Trestle) talks to a flaky external service. A scraper run processes hundreds of records; one HTTP timeout, one OCR garble, or one LLM rate-limit cannot abort the whole pipeline. The idiom guarantees forward progress.

When to use it:
- ANY external API call (HTTP, browser automation, LLM)
- Per-record loops in enrichers (each iteration wraps in try/except, never the whole loop)
- Optional/best-effort steps (e.g. drive_uploader, slack_notifier — these `try/except Exception: pass` silently)

When NOT to use it:
- Programming errors (KeyError on a required dict key, TypeError on bad arguments) — let those crash so they get fixed
- Required setup (config validation in `_preflight_check`, see `src/main.py:52-100`) — these return a `failures: list[str]` and the caller decides whether to continue
- Cryptographic / auth flows where silent failure would hide a real security issue

**Use `logger.exception` (not `logger.error`) inside `except Exception:`** when you don't have a more specific handler — it auto-includes the traceback. `logger.warning` is used for expected, recoverable failures (e.g. "address not found in tax roll"); `logger.error` is used when something the operator should investigate has happened.

**Never use bare `except:`** — always `except Exception:` or a specific subclass. `except Exception` is the standard choice when you don't know what the upstream library throws.

## Logging

**Framework:** Python stdlib `logging` — no structlog, loguru, or other.

**Setup pattern (every module):**
```python
import logging
logger = logging.getLogger(__name__)
```
52 of 54 source modules in `src/` have this exact line. Use module-level `logger`, never `logging.<level>(...)` directly in module code (CLI scripts in `tests/` and `test_*.py` use `logging.basicConfig(...)` for setup only).

**Setup is centralized in `src/main.py`** — `setup_logging(verbose: bool)` configures both stream and file handlers with timestamped log files in `logs/`. Verbosity is controlled by the `-v` CLI flag.

**Log levels:**
- `logger.debug`: per-record details, regex match traces, dropped/filtered records (`logger.debug("  Filtered out (wrong county): %s", notice_id)`)
- `logger.info`: pipeline progress, per-step counts, per-county summaries (`logger.info("  Removed %d vacant land records", removed)`)
- `logger.warning`: recoverable failures the user should know about (HTTP retries, LLM fallback triggered, missing optional credentials)
- `logger.error`: failures that block the current operation but not the run (CAPTCHA exhaustion on a single notice)
- `logger.exception`: only inside `except Exception:` blocks where the traceback is wanted

**Format strings always use `%`-style with separate args**, NEVER f-strings:
```python
# Correct — lazy formatting; the logger skips formatting if level is filtered
logger.warning("KGIS lookup failed for '%s': %s", name, e)

# Wrong — formats every time even if DEBUG is off
logger.warning(f"KGIS lookup failed for '{name}': {e}")
```
This is followed consistently — `grep` for `logger.\w*\(f"` returns essentially nothing.

## Imports

**Order (PEP 8):**
1. Standard library (`import json`, `import logging`, `import re`, etc.)
2. Third-party (`import httpx`, `from playwright.async_api import Page, ...`)
3. First-party / project modules (`import config`, `from notice_parser import NoticeData`)

Each group separated by a blank line.

**Style:**
- Imports are typically NOT relative — modules import from siblings as if `src/` were on `PYTHONPATH` (it is, when run from project root with `python src/main.py`). E.g. `from notice_parser import NoticeData`, not `from .notice_parser import NoticeData`.
- `from X import Y, Z` over `import X` for project modules; full-module `import X` for stdlib (`import logging`, `import json`, `import re`)
- One name per line for long import lists (see `src/main.py:17-21`, `src/scraper.py:13-36`)
- `from config import (...)` blocks are common and explicitly list every name (no `from config import *`)

## Function Design

**Size:** Functions in this codebase are large by modern standards — `parse_notice_page` in `src/notice_parser.py` exceeds 200 lines, `_parse_address` is regex-heavy and ~100 lines. Long functions are tolerated when they're sequential parsing/extraction logic with no useful sub-decomposition.

**Parameters:** Keyword-only arguments are used for "options" (after `*`):
```python
def search_by_owner_name(
    last_name: str,
    first_name: str | None = None,
    *,
    year: int = 2025,
    use_contains: bool = False,
    real_property_only: bool = True,
) -> list[MadisonPropertyRecord]:
```
See `src/madison_property_api.py:179`. The pattern: required positional args, then `*`, then optional flags. This protects against silent breakage when a new option is added.

**Docstrings:**
- Module-level: ALWAYS present. First line is a one-sentence summary; the rest explains domain context, file format, or external service quirks. See `src/notice_parser.py:1-16`, `src/madison_property_api.py:1-17`.
- Function-level: present on public API functions, often missing on private helpers.
- Style: Google-ish (`Args:`, `Returns:`, `Raises:`) but loose. Plain prose docstrings are also acceptable — see `src/madison_property_api.py:179-202`.

**Returns:**
- `list[T]` for "many results" (always a list, never a generator at API boundaries — generators only used internally as `_iter_records` style helpers)
- `dict` (untyped) for ad-hoc result envelopes from Playwright workflows (`{success: bool, message: str, ...}`) — see `datasift_uploader.upload_csv` returning `dict`
- `bool` for predicates (functions starting with `is_`, `has_`, `should_`)
- Empty list / empty string / `None` for "not found", never raise — let the caller decide

## Comments

**Liberal contextual comments are the norm.** This codebase is heavily commented at the *why* level — every regex pattern, every empirical threshold, every API quirk is annotated. Examples:

```python
# src/jefferson_property_api.py:194-198
# NOTE: jeffersonexpress.capturecama.com serves an incomplete SSL chain —
# browsers/curl-system trust resolve the intermediate via macOS/Windows
# keychain, but Python's certifi bundle does not. The host is fixed and
# owned by Jefferson County's vendor, so verify=False is acceptable here;
# the alternative would be shipping the intermediate bundle.
return httpx.Client(timeout=timeout, follow_redirects=True, verify=False, ...)
```

```python
# src/config.py:181-196 — entire "code-violation" search-config block has
# 16 lines of comment explaining false-positive rates of naive keywords
```

When adding new code:
- ALWAYS comment the *why* of any non-obvious choice (empirical thresholds, regex tradeoffs, why a workaround exists)
- ALWAYS comment SSL / auth / timeout / retry decisions
- Inline comments on regex patterns explaining what the pattern accepts/rejects
- "DON'T" comments are common and welcomed — `# DO NOT use fix_rotation() (Tesseract OSD) on phone photos`

## Module Design

**Exports:** No `__all__` declarations anywhere — Python's default "everything not underscore-prefixed is public" governs.

**Barrel files:** None. There is no `src/__init__.py` exporting names from sub-modules; every consumer imports directly from the module that defines the symbol. (`src/__init__.py` does not exist; `src/__main__.py` is the Apify Actor entry point only.)

**`if __name__ == "__main__":` blocks:** Most modules that have any standalone use (county adapters, pipelines, the property locator) include a CLI entry point at the bottom. The convention:
```python
def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ...
    return 0

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
```
This makes every adapter independently testable — see `src/madison_property_api.py:329-357`.

## Adding New SearchConfig Entries

`SAVED_SEARCHES` is the central registry of keyword searches against alabamapublicnotices.com — defined as a `list[SearchConfig]` at `src/config.py:129-285`. To add a new search:

1. **Append** a new `SearchConfig(...)` to the list. Order is not significant for filtering (Filter by `--counties` / `--types` flags happens in `_filter_searches` in `src/main.py:31-46`).
2. **Required fields:** `county`, `notice_type`, `search_terms`, `search_type` (`"AND"` or `"OR"`), `exclude_terms`, `days_back`.
3. **Optional `notice_subtype`:** when set, every notice from this search is auto-stamped with `notice.notice_subtype = <value>`. Used today for code-violation searches (`notice_subtype="unsafe_building"`) so the DataSift formatter automatically fires the `demolish` tag.
4. **Comment your keyword choice.** Any new search must include a block comment explaining empirical false-positive rates of alternatives considered (see the eviction-search rejection block at `src/config.py:251-285` — it's left in the file as a permanent record of why APN doesn't carry evictions).
5. **County-of-property check:** APN's search box has no county filter. Filtering by property location happens in `is_target_county()` against the full notice text post-CAPTCHA. The `county` field on `SearchConfig` filters which results to keep, NOT which results the search returns.

**Example (the canonical structure):**
```python
SearchConfig(
    county="Jefferson",
    notice_type="code_violation",
    notice_subtype="unsafe_building",
    search_terms="DEMOLITION UNSAFE STRUCTURE",
    search_type="AND",
    exclude_terms="bid contractor sealed",
    days_back=14,
),
```

## Adding a New County Adapter (the Madison/Jefferson Pair Pattern)

When extending coverage to a new county, follow the established pair-of-adapters pattern. Files: `src/madison_property_api.py` + `src/jefferson_property_api.py` (probate-property lookup), `src/madison_tax_delinquent_api.py` + `src/jefferson_tax_delinquent_api.py` (tax distress), `src/huntsville_unsafe_buildings_api.py` + `src/birmingham_code_enforcement_api.py` (code violations).

**Required structure:**

1. **Module docstring** — explain the upstream data source, URL, auth requirements (or lack thereof), and any quirks (e.g. SSL chain issues, WAF fingerprinting, ASP.NET ViewState requirements).

2. **Frozen dataclass record** — `@dataclass(frozen=True) class <County><Type>Record` with real types (`float`, `int`, `bool`), a `from_<source>_record(cls, raw: dict)` classmethod, and derived flags (`is_buildable`, `is_homestead`, `is_delinquent`, `is_high_exposure`).

3. **Cross-county field parity** — the new dataclass must expose the same fields as its sibling, even if some are stubbed. Document stubs explicitly:
   ```python
   municipality: str  # Always "" for Madison — AssuranceWeb search response doesn't expose city
   ```

4. **HTTP layer** — prefer `httpx.Client` over `requests` for new code (the Madison/Jefferson adapters all use httpx). Use `_new_client()` helper to centralize timeout + headers + (rarely) `verify=False`. Falls back to `requests` only when httpx triggers a WAF block (`src/huntsville_unsafe_buildings_api.py` uses `requests` for this reason).

5. **Public API functions:**
   - `search_by_owner_name(last_name, first_name=None, *, year=..., ...) -> list[<Record>]`
   - `search_by_situs_address(street_number, street_name, *, year=...) -> list[<Record>]` (when supported)
   - `fetch_delinquent_parcels(...) -> list[<Record>]` for bulk-list adapters
   - `to_notice_data(rec: <Record>) -> NoticeData` to convert to the canonical pipeline schema

6. **CLI entry point** — `_main(argv: list[str]) -> int` at the bottom, dispatched by `if __name__ == "__main__":`.

7. **Wire into the unified pipeline** — add a `_fetch_<county>()` private function and a fan-out call in the orchestrator (`src/tax_distress_pipeline.py`, `src/code_violation_pipeline.py`).

8. **Document in CLAUDE.md** — add a section explaining the adapter, the per-county quirks, and DataSift filter-preset compositions that target the new data.

## Field Mapping Quirks (Worth Knowing)

These are intentional design choices, not bugs. New code should follow them, not "fix" them.

- **`tax_delinquent_amount` is overloaded for code-violation fees.** Birmingham Accela code-enforcement records map their `fee_total` / `fee_balance` into `NoticeData.tax_delinquent_amount` because DataSift's standard 80-column schema has no separate "code-violation fees" column. The same `tax_high_exposure` filter-preset tag fires when fees ≥ $5K, surfacing high-exposure code-violation balances alongside tax delinquencies. See CLAUDE.md "Birmingham Accela early-distress scraper" → "Detail-page enrichment".

- **Probate `Probate Open Date` prefers `granted_date` over `date_added`.** When AL Letters Testamentary were issued (per AL § 43-2-61) is more useful than the publication date. The DataSift formatter falls back to publication date only when granted_date is missing. (`src/datasift_formatter.py`)

- **Probate `Personal Representative` uses `owner_name`, not `decision_maker_name`.** For probate notices, the verified PR named in the Letters takes precedence over an obituary-derived heir.

- **`Estimated Value` falls back to `assessed_value`.** When Zillow's `estimated_value` hasn't been populated by downstream enrichment, the assessor's last-assessed value fills the slot. Same idea for `Structure Type` ← `property_use`.

- **Personal trusts and estates are NOT business entities.** `_filter_entity_owners` exempts records where the personal-trust regex matches — `JOHN DOE TRUST` is a person, but `FIRST TENNESSEE BANK TRUST` is a business. (`src/enrichment_pipeline.py:142-150`, `src/config.py` `BUSINESS_RE` + `TRUST_NAME_RE`)

- **`tax_delinquent_years` is empty for Madison records.** Madison's delinquent feed is current-year-only by design — older years are pruned after the May auction or redemption. Storing a misleading "0" or year value would cause confusion. The assessment year is preserved in `raw_text` for human-readable inspection. (`src/madison_tax_delinquent_api.py`)

- **Empty strings, never `None`.** `NoticeData` defaults every field to `""`. Downstream code uses `if notice.address:` or `notice.address.strip()` freely — never `if notice.address is not None`. New fields added to `NoticeData` MUST default to `""` (or `"0"` for stringly-typed numbers), never `None`.

- **Smart quotes get normalized first.** `_normalize_pdf_text()` in `src/notice_parser.py` de-hyphenates column-wrapped words AND replaces curly quotes — without the latter, every regex matching `'` in `("Transferee")` fails on Madison newspaper PDFs.

---

*Convention analysis: 2026-04-30*
