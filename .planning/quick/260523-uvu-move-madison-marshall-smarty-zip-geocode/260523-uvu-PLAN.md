---
phase: 260523-uvu
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/address_standardizer.py
  - src/pre_probate_pipeline_al.py
  - src/apn_probate_pipeline_al.py
  - src/property_lookup.py
autonomous: true
requirements:
  - QUICK-260523-uvu

must_haves:
  truths:
    - "Public helpers smarty_zip_for_assuranceweb_address / smarty_zip_for_madison_address / smarty_zip_for_marshall_address are importable from src.address_standardizer"
    - "Legacy underscore-prefixed names (_smarty_zip_for_madison_address, _smarty_zip_for_marshall_address, _smarty_zip_for_assuranceweb_address) still importable from src.pre_probate_pipeline_al for backward compat"
    - "apn_probate_pipeline_al imports from address_standardizer (not pre_probate_pipeline_al) and call sites at lines 215-221 reference the public names"
    - "property_lookup.py county allowlist accepts marshall in addition to jefferson + madison"
    - "After enrich_notice_with_property() succeeds in the legacy main.py daily flow, Madison + Marshall notices with address-but-no-ZIP get a Smarty geocode that fills notice.zip (and notice.city when empty)"
    - "Live smoke test: smarty_zip_for_madison_address('100 Church St Sw') returns a non-empty 5-digit ZIP using .env-loaded Smarty creds"
  artifacts:
    - path: "src/address_standardizer.py"
      provides: "Public Smarty ZIP-recovery helpers for Madison + Marshall AssuranceWeb addresses"
      contains: "def smarty_zip_for_assuranceweb_address"
    - path: "src/address_standardizer.py"
      provides: "Madison-anchored convenience wrapper"
      contains: "def smarty_zip_for_madison_address"
    - path: "src/address_standardizer.py"
      provides: "Marshall-anchored convenience wrapper"
      contains: "def smarty_zip_for_marshall_address"
    - path: "src/pre_probate_pipeline_al.py"
      provides: "Backward-compat aliases for the three relocated helpers"
      contains: "_smarty_zip_for_madison_address = smarty_zip_for_madison_address"
    - path: "src/apn_probate_pipeline_al.py"
      provides: "Updated import + call sites pointing at address_standardizer"
      contains: "from address_standardizer import"
    - path: "src/property_lookup.py"
      provides: "Marshall in county allowlist + post-enrichment Smarty geocode for Madison/Marshall"
      contains: "marshall"
  key_links:
    - from: "src/pre_probate_pipeline_al.py"
      to: "src/address_standardizer.py"
      via: "module-level alias re-exports"
      pattern: "_smarty_zip_for_.*_address = smarty_zip_for"
    - from: "src/apn_probate_pipeline_al.py"
      to: "src/address_standardizer.py"
      via: "from address_standardizer import smarty_zip_for_madison_address, smarty_zip_for_marshall_address"
      pattern: "from address_standardizer import .*smarty_zip_for"
    - from: "src/property_lookup.py"
      to: "src/address_standardizer.py"
      via: "post-enrichment Smarty geocode call"
      pattern: "smarty_zip_for_(madison|marshall)_address"
---

<objective>
Move the three Smarty ZIP-recovery helpers (`_smarty_zip_for_assuranceweb_address`, `_smarty_zip_for_madison_address`, `_smarty_zip_for_marshall_address`) plus their supporting `_smarty_lookup_once` shim and anchor-city tuples from `src/pre_probate_pipeline_al.py` into the shared `src/address_standardizer.py` toolbox under public (non-underscore) names. Keep one-line backward-compat aliases in `pre_probate_pipeline_al.py`. Update `apn_probate_pipeline_al.py` to import from the new location. Then close the actual production gap: extend `property_lookup.py` (the legacy `main.py daily` consumer) to (a) include `marshall` in its county allowlist alongside `jefferson` and `madison`, and (b) post-enrich Madison + Marshall notices with the matching Smarty geocode helper so probate notices with a situs but no ZIP stop getting dropped at the downstream tier filter.

Purpose: Two of our three active post-probate paths (`apn_probate_pipeline_al.py` and `pre_probate_pipeline_al.py`) already use these helpers and recover the missing ZIP, but the legacy `main.py daily` flow runs through `property_lookup.py` and silently drops every Madison + Marshall probate notice whose tax-roll match lacks a ZIP (which is most of them — the AssuranceWeb name-search response only returns street). Marshall isn't even in the allowlist yet, so it's a full drop before we get to ZIP-gating at all. Centralizing the helpers also retires the cross-module underscore-prefixed import pattern that has `apn_probate_pipeline_al.py` reaching into a sibling pipeline's private namespace.

Output: Three helpers + their support code live in `address_standardizer.py` under public names; `pre_probate_pipeline_al.py` exports legacy underscore aliases; `apn_probate_pipeline_al.py` imports the public names; `property_lookup.py` handles Marshall and fills missing ZIPs for Madison + Marshall right after `enrich_notice_with_property()` succeeds.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@CLAUDE.md

# Source of the helpers (read lines 448-565 for anchors + _smarty_lookup_once + the three public-helper definitions)
@src/pre_probate_pipeline_al.py

# Destination toolbox (already imports the smartystreets SDK and exposes _build_client)
@src/address_standardizer.py

# Current consumer that imports the underscore-prefixed helpers (lines 50-56) and uses them at lines 213-224
@src/apn_probate_pipeline_al.py

# Legacy daily-flow consumer — line 406 allowlist + the enrich-and-continue block around lines 405-425
@src/property_lookup.py

<interfaces>
<!-- Existing public surface of address_standardizer.py that this plan extends -->

From src/address_standardizer.py:
```python
def _build_client(auth_id: str, auth_token: str)  # used by _smarty_lookup_once
def _build_lastline(notice: NoticeData) -> str
def standardize_addresses(notices: list[NoticeData], auth_id: str, auth_token: str) -> list[NoticeData]
def retry_with_geocoded_city(notices: list[NoticeData], auth_id: str, auth_token: str) -> None
```

From src/pre_probate_pipeline_al.py (CURRENT location — these move to address_standardizer):
```python
_MADISON_ANCHORS: tuple[str, ...]   # 12 entries, "Huntsville AL" first, "AL" last
_MARSHALL_ANCHORS: tuple[str, ...]  # 8 entries, "Albertville AL" first, "AL" last

def _smarty_lookup_once(situs: str, lastline_hint: str) -> tuple[str, str]
def _smarty_zip_for_assuranceweb_address(
    situs: str,
    lastline_hint: str = "Huntsville AL",
    anchor_fallbacks: tuple[str, ...] | None = None,
) -> tuple[str, str]
def _smarty_zip_for_madison_address(situs: str) -> tuple[str, str]
def _smarty_zip_for_marshall_address(situs: str) -> tuple[str, str]
```

New public names (post-move, no other signature changes):
```python
# in src/address_standardizer.py
smarty_zip_for_assuranceweb_address(situs, lastline_hint="Huntsville AL", anchor_fallbacks=None) -> tuple[str, str]
smarty_zip_for_madison_address(situs) -> tuple[str, str]
smarty_zip_for_marshall_address(situs) -> tuple[str, str]
```

From src/apn_probate_pipeline_al.py — current import block (lines 50-55):
```python
from pre_probate_pipeline_al import (
    _normalize_decedent_key,
    _promote_heir_contacts_to_csv_slots,
    _smarty_zip_for_madison_address,
    _smarty_zip_for_marshall_address,
)
```

Call sites at lines 215-221:
```python
if county_lc == "marshall":
    city, zip_code = _smarty_zip_for_marshall_address(n.address)
else:
    city, zip_code = _smarty_zip_for_madison_address(n.address)
```

From src/property_lookup.py — current allowlist (line ~406):
```python
if notice.county.lower() in ("jefferson", "madison"):
    from probate_property_locator import enrich_notice_with_property
    matched = enrich_notice_with_property(notice)
```
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Relocate Smarty ZIP helpers + anchor tuples into address_standardizer.py with backward-compat aliases</name>
  <files>src/address_standardizer.py, src/pre_probate_pipeline_al.py</files>
  <action>
1. Append the helpers to `src/address_standardizer.py` (after `retry_with_geocoded_city`, end of file). Add them under PUBLIC names (no underscore prefix):
   - `_MADISON_ANCHORS` and `_MARSHALL_ANCHORS` — copy the exact tuples from `pre_probate_pipeline_al.py` lines 453-467 verbatim (these constants stay underscore-prefixed because they're internal anchor data, not part of the helper API).
   - `_smarty_lookup_once(situs, lastline_hint)` — copy verbatim from pre_probate_pipeline_al.py lines 470-493. It already does `from address_standardizer import _build_client` — change that to a direct local call (just call `_build_client(...)` since we're now IN address_standardizer.py). Drop the now-redundant import line.
   - `smarty_zip_for_assuranceweb_address(situs, lastline_hint="Huntsville AL", anchor_fallbacks=None)` — copy from lines 496-540 but RENAME (strip leading underscore). Preserve the full docstring verbatim — it documents the multi-anchor fallback strategy and references the live-run bugs it fixes; future readers need that context.
   - `smarty_zip_for_madison_address(situs)` — copy from lines 543-549, strip underscore from public name AND update its inner call to use the (now public) `smarty_zip_for_assuranceweb_address`.
   - `smarty_zip_for_marshall_address(situs)` — copy from lines 552-564, strip underscore from public name AND update its inner call to use the (now public) `smarty_zip_for_assuranceweb_address`.

2. In `src/pre_probate_pipeline_al.py`:
   - DELETE the original definitions at lines 448-564 (the anchor tuples, `_smarty_lookup_once`, and all three `_smarty_zip_for_*` functions).
   - In their place add a small re-export block. Keep these as one-liner aliases so any external caller (or our own `apn_probate_pipeline_al.py` until Task 2 lands) still resolves the legacy names. Aim for something like:
     ```python
     # ── Backward-compat re-exports ────────────────────────────────────
     # These helpers moved to address_standardizer.py on 2026-05-23.
     # Aliases kept so external callers and the apn_probate_pipeline_al
     # legacy import path keep working. New code should import from
     # address_standardizer directly.
     from address_standardizer import (
         smarty_zip_for_assuranceweb_address,
         smarty_zip_for_madison_address,
         smarty_zip_for_marshall_address,
     )
     _smarty_zip_for_assuranceweb_address = smarty_zip_for_assuranceweb_address
     _smarty_zip_for_madison_address = smarty_zip_for_madison_address
     _smarty_zip_for_marshall_address = smarty_zip_for_marshall_address
     ```
   - Place that block at module scope at the SAME line range where the originals lived (so anyone grepping line-anchored history finds the rename in one diff hunk).

3. Sanity-check: `_smarty_lookup_once` is NOT re-exported from `pre_probate_pipeline_al.py` because nothing outside that file ever imported it (it was a true private helper). Confirm with `grep -rn "_smarty_lookup_once" src/` — should return matches only in `address_standardizer.py` after the move.

4. Do NOT touch any other code in `pre_probate_pipeline_al.py`. Specifically: the orchestrator stages that call `_smarty_zip_for_madison_address` for Madison hits in `_attach_property_for_decedent()` (and the Marshall equivalent) will continue to resolve through the alias and keep working unchanged.
  </action>
  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack && PYTHONPATH=src python -c "from address_standardizer import smarty_zip_for_madison_address, smarty_zip_for_marshall_address, smarty_zip_for_assuranceweb_address; from pre_probate_pipeline_al import _smarty_zip_for_madison_address, _smarty_zip_for_marshall_address, _smarty_zip_for_assuranceweb_address; assert _smarty_zip_for_madison_address is smarty_zip_for_madison_address; assert _smarty_zip_for_marshall_address is smarty_zip_for_marshall_address; assert _smarty_zip_for_assuranceweb_address is smarty_zip_for_assuranceweb_address; print('OK: helpers moved + aliases wired')"</automated>
  </verify>
  <done>
- `src/address_standardizer.py` defines `smarty_zip_for_assuranceweb_address`, `smarty_zip_for_madison_address`, `smarty_zip_for_marshall_address`, plus the `_smarty_lookup_once` helper and `_MADISON_ANCHORS` / `_MARSHALL_ANCHORS` tuples.
- `src/pre_probate_pipeline_al.py` exposes `_smarty_zip_for_madison_address`, `_smarty_zip_for_marshall_address`, `_smarty_zip_for_assuranceweb_address` as aliases to the public names, with an inline comment explaining the re-export.
- Verification command imports both old and new names and asserts `is`-identity (alias, not copy).
- No other behavior changes in either file.
  </done>
</task>

<task type="auto">
  <name>Task 2: Switch apn_probate_pipeline_al.py to import from address_standardizer + live Smarty smoke test</name>
  <files>src/apn_probate_pipeline_al.py</files>
  <action>
1. In `src/apn_probate_pipeline_al.py`, edit the import block at lines 50-55. Replace:
   ```python
   from pre_probate_pipeline_al import (
       _normalize_decedent_key,
       _promote_heir_contacts_to_csv_slots,
       _smarty_zip_for_madison_address,
       _smarty_zip_for_marshall_address,
   )
   ```
   with two import statements (preserve `_normalize_decedent_key` and `_promote_heir_contacts_to_csv_slots` from `pre_probate_pipeline_al` since those are unrelated helpers staying put):
   ```python
   from pre_probate_pipeline_al import (
       _normalize_decedent_key,
       _promote_heir_contacts_to_csv_slots,
   )
   from address_standardizer import (
       smarty_zip_for_madison_address,
       smarty_zip_for_marshall_address,
   )
   ```

2. Update the call sites in the same file at lines 215-221. Replace the underscore-prefixed calls with the public names:
   ```python
   if county_lc == "marshall":
       city, zip_code = smarty_zip_for_marshall_address(n.address)
   else:
       city, zip_code = smarty_zip_for_madison_address(n.address)
   ```
   (Two character changes — drop the leading underscore on each function call.)

3. Confirm with `grep -n "_smarty_zip_for" src/apn_probate_pipeline_al.py` — should return ZERO matches after the edit. Then `grep -n "smarty_zip_for" src/apn_probate_pipeline_al.py` should return exactly four matches (two in the import block, two in the call sites).

4. Live smoke test (uses Smarty creds from `.env`, key rotated 2026-05-23 — confirmed working today). DO NOT run the full daily scrape; this is a single-address geocode that completes in <1 second:
   ```bash
   cd /Users/shanismith/Desktop/SiftStack && PYTHONPATH=src python -c "
   from dotenv import load_dotenv; from pathlib import Path
   load_dotenv(Path.home() / 'Desktop/SiftStack/.env')
   from address_standardizer import smarty_zip_for_madison_address, smarty_zip_for_marshall_address
   city_m, zip_m = smarty_zip_for_madison_address('100 Church St Sw')
   print(f'Madison: 100 Church St Sw -> city={city_m!r} zip={zip_m!r}')
   assert zip_m and len(zip_m) == 5 and zip_m.isdigit(), f'Madison Smarty returned bad zip: {zip_m!r}'
   # Also verify the import path apn_probate_pipeline_al uses now resolves:
   from apn_probate_pipeline_al import smarty_zip_for_madison_address as via_apn
   assert via_apn is smarty_zip_for_madison_address, 'apn_probate_pipeline_al not re-exporting from address_standardizer'
   print('OK: live Smarty hit + apn import path verified')
   "
   ```
  </action>
  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack && grep -c "_smarty_zip_for" src/apn_probate_pipeline_al.py | grep -q "^0$" && grep -c "smarty_zip_for" src/apn_probate_pipeline_al.py | grep -q "^4$" && PYTHONPATH=src python -c "from dotenv import load_dotenv; from pathlib import Path; load_dotenv(Path.home() / 'Desktop/SiftStack/.env'); from address_standardizer import smarty_zip_for_madison_address; c, z = smarty_zip_for_madison_address('100 Church St Sw'); assert z and len(z) == 5 and z.isdigit(), f'bad zip {z!r}'; print(f'OK: Madison geocode returned zip={z}')"</automated>
  </verify>
  <done>
- `src/apn_probate_pipeline_al.py` imports `smarty_zip_for_madison_address` + `smarty_zip_for_marshall_address` from `address_standardizer` (NOT from `pre_probate_pipeline_al`).
- Both call sites at lines 215-221 use the public (non-underscore) names.
- `grep -c "_smarty_zip_for" src/apn_probate_pipeline_al.py` returns 0.
- Live smoke test returns a non-empty 5-digit ZIP for `100 Church St Sw` using current `.env` Smarty creds.
  </done>
</task>

<task type="auto">
  <name>Task 3: Fix property_lookup.py — add marshall to allowlist + post-enrich Smarty geocode for Madison/Marshall</name>
  <files>src/property_lookup.py</files>
  <action>
This is the actual production gap-close: the legacy `main.py daily` flow currently drops every Madison + Marshall probate notice whose tax-roll match comes back without a ZIP (which is most of them, because AssuranceWeb's name-search response only returns the situs street). Marshall is also missing from the allowlist entirely.

1. Locate the county-allowlist branch in `src/property_lookup.py` at line ~406. The current line reads:
   ```python
   if notice.county.lower() in ("jefferson", "madison"):
   ```
   Replace with:
   ```python
   if notice.county.lower() in ("jefferson", "madison", "marshall"):
   ```
   The body of this branch (which calls `enrich_notice_with_property()`) already works for all three counties because `probate_property_locator.enrich_notice_with_property()` already dispatches to `_search_jefferson` / `_search_madison` / `_search_marshall` internally — no other change needed inside the branch dispatch itself.

2. Inside the same `if`-branch, AFTER the line `matched = enrich_notice_with_property(notice)` and AFTER the existing `if matched: logger.info(...); found += 1` success-path (around lines 414-420 — read those lines first to see the exact `if matched:` block structure), insert a post-enrichment Smarty geocode for Madison + Marshall when the locator filled an address but no ZIP. Add this block INSIDE the `if matched:` branch, BEFORE the `await asyncio.sleep(...)` call at line 424:
   ```python
   # Madison + Marshall AssuranceWeb name-search responses return
   # only the street — city/zip aren't in the bulk payload. Without
   # this, downstream tier filtering drops the notice. Same fix the
   # apn_probate_pipeline_al + pre_probate_pipeline_al pipelines use.
   county_lc = notice.county.lower()
   if county_lc in ("madison", "marshall") and notice.address and not notice.zip:
       from address_standardizer import (
           smarty_zip_for_madison_address,
           smarty_zip_for_marshall_address,
       )
       helper = (
           smarty_zip_for_marshall_address
           if county_lc == "marshall"
           else smarty_zip_for_madison_address
       )
       city, zip_code = helper(notice.address)
       if zip_code:
           notice.zip = zip_code
           if not notice.city and city:
               notice.city = city
           logger.info(
               "  Smarty filled %s ZIP: %s -> %s, %s",
               notice.county, notice.address, city, zip_code,
           )
       else:
           logger.info(
               "  Smarty could not resolve ZIP for %s %s (kept address, no zip)",
               notice.county, notice.address,
           )
   ```
   Function-scoped import is intentional (mirrors the existing pattern at line 413 `from probate_property_locator import enrich_notice_with_property`) so the legacy daily flow's startup cost stays unchanged for non-probate notice types.

3. Do NOT touch the Knox / Blount branches or any other county logic. Do NOT change `enrich_notice_with_property()` itself. Do NOT remove the `await asyncio.sleep(random.uniform(2.0, 3.0))` rate-limit that follows — it stays in place after the new Smarty block.

4. Verification commands (NO full scrape — single-notice in-memory test):
   ```bash
   # Allowlist + import check (no API calls, fast)
   cd /Users/shanismith/Desktop/SiftStack && PYTHONPATH=src python -c "
   import ast, pathlib
   src = pathlib.Path('src/property_lookup.py').read_text()
   assert '\"jefferson\", \"madison\", \"marshall\"' in src or \"'jefferson', 'madison', 'marshall'\" in src, 'marshall not in allowlist'
   assert 'smarty_zip_for_madison_address' in src, 'Madison Smarty helper not imported'
   assert 'smarty_zip_for_marshall_address' in src, 'Marshall Smarty helper not imported'
   ast.parse(src)  # syntax check
   print('OK: allowlist + helpers present, syntax valid')
   "
   ```
  </action>
  <verify>
    <automated>cd /Users/shanismith/Desktop/SiftStack && PYTHONPATH=src python -c "
import ast, pathlib
src = pathlib.Path('src/property_lookup.py').read_text()
assert '\"jefferson\", \"madison\", \"marshall\"' in src or \"'jefferson', 'madison', 'marshall'\" in src, 'marshall not in allowlist'
assert 'smarty_zip_for_madison_address' in src, 'Madison Smarty helper not imported'
assert 'smarty_zip_for_marshall_address' in src, 'Marshall Smarty helper not imported'
ast.parse(src)
import property_lookup  # full import to catch any indentation/scope errors
print('OK: allowlist updated, helpers imported, module imports clean')
"</automated>
  </verify>
  <done>
- `src/property_lookup.py` allowlist at line ~406 now reads `("jefferson", "madison", "marshall")`.
- Inside the AL-county success branch (after `enrich_notice_with_property()` returns True), Madison + Marshall notices with an address but no ZIP get a Smarty geocode that fills `notice.zip` (and `notice.city` when empty).
- Smarty failure path logs informatively and keeps the notice (does NOT drop) — downstream tier filter will handle the empty-ZIP case as it does today.
- `import property_lookup` succeeds; no syntax / indentation errors introduced.
- No changes to Knox / Blount / Tennessee branches or to `enrich_notice_with_property()` itself.
  </done>
</task>

</tasks>

<verification>
End-to-end import + behavior verification (no full pipeline run — daily scrape takes hours, explicitly out of scope per quick-mode constraints):

```bash
cd /Users/shanismith/Desktop/SiftStack && PYTHONPATH=src python -c "
from dotenv import load_dotenv; from pathlib import Path
load_dotenv(Path.home() / 'Desktop/SiftStack/.env')

# 1. New public path
from address_standardizer import (
    smarty_zip_for_assuranceweb_address,
    smarty_zip_for_madison_address,
    smarty_zip_for_marshall_address,
)

# 2. Legacy private path (backward compat)
from pre_probate_pipeline_al import (
    _smarty_zip_for_assuranceweb_address,
    _smarty_zip_for_madison_address,
    _smarty_zip_for_marshall_address,
)
assert _smarty_zip_for_madison_address is smarty_zip_for_madison_address

# 3. apn_probate_pipeline_al now imports from address_standardizer
import apn_probate_pipeline_al as apn
assert apn.smarty_zip_for_madison_address is smarty_zip_for_madison_address

# 4. property_lookup imports cleanly + has marshall + the helpers
import property_lookup as pl
src = Path('src/property_lookup.py').read_text()
assert 'marshall' in src.lower()
assert 'smarty_zip_for_marshall_address' in src

# 5. Live Smarty hit (single address, ~0.5s, ~\$0.001)
city, zip_ = smarty_zip_for_madison_address('100 Church St Sw')
print(f'Madison live: 100 Church St Sw -> city={city!r} zip={zip_!r}')
assert zip_ and len(zip_) == 5 and zip_.isdigit()

print('ALL VERIFICATION PASSED')
"
```

Expected output ends with `ALL VERIFICATION PASSED`. If the live Smarty hit returns an empty zip, the .env key has rotated or rate-limited — re-check credentials, do NOT proceed.
</verification>

<success_criteria>
- All three Smarty helpers live at `src/address_standardizer.py` under public names; the originals are gone from `pre_probate_pipeline_al.py`.
- `pre_probate_pipeline_al.py` re-exports the three legacy underscore names via module-scope aliases; comment explains they're shims.
- `apn_probate_pipeline_al.py` imports the public names from `address_standardizer` (NOT from `pre_probate_pipeline_al`); both call sites updated.
- `property_lookup.py` includes `"marshall"` in its AL-county allowlist AND post-enriches Madison + Marshall notices with a Smarty geocode when address is set but ZIP isn't, mirroring the existing pattern in `apn_probate_pipeline_al.py` lines 213-224.
- End-to-end verification script (above) exits with `ALL VERIFICATION PASSED` and a non-empty 5-digit ZIP for the Madison smoke address.
- No changes to `enrich_notice_with_property()`, Knox/Blount branches, Smarty `standardize_addresses` batch path, or anchor city lists (data preserved verbatim during the move).
</success_criteria>

<output>
After completion, create `.planning/quick/260523-uvu-move-madison-marshall-smarty-zip-geocode/260523-uvu-SUMMARY.md` documenting: (1) files moved/changed with before/after import paths, (2) result of the live Smarty smoke-test ZIP, (3) confirmation that the Madison + Marshall ZIP-recovery fix is now wired into the legacy `main.py daily` flow via `property_lookup.py` (this is the actual production behavior change — the helper move is mechanical refactor), and (4) any addresses that the smoke test surfaced as Smarty-unresolvable (so we know the failure path logs cleanly).
</output>
