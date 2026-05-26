---
phase: 260523-uvu
plan: 01
subsystem: address-standardization, post-probate pipelines
tags: [refactor, smarty, madison, marshall, property-lookup, pre-probate, apn-probate]
dependency-graph:
  requires:
    - src/address_standardizer.py (existing _build_client + smartystreets SDK wiring)
    - .env Smarty credentials (rotated 2026-05-23, verified working)
  provides:
    - smarty_zip_for_assuranceweb_address / smarty_zip_for_madison_address / smarty_zip_for_marshall_address as public surface of src/address_standardizer
    - property_lookup.py post-enrichment Smarty geocode for Madison + Marshall in the legacy main.py daily flow
  affects:
    - src/pre_probate_pipeline_al.py (private definitions removed, replaced with re-export aliases)
    - src/apn_probate_pipeline_al.py (import path swapped to address_standardizer)
    - src/property_lookup.py (Marshall added to allowlist + new post-enrichment Smarty step)
    - src/distress_proxy_pipeline.py (unchanged — still imports underscore aliases from pre_probate_pipeline_al, which now resolve through the alias re-export)
tech-stack:
  added: []
  patterns:
    - Public re-export aliases for backward compat across module relocations
    - Function-scoped imports for cross-pipeline helpers to keep startup cost low for unrelated notice types
key-files:
  created: []
  modified:
    - src/address_standardizer.py (+125 lines: 3 public helpers + _smarty_lookup_once + anchor tuples)
    - src/pre_probate_pipeline_al.py (-117 / +17 lines: helpers replaced with re-export block)
    - src/apn_probate_pipeline_al.py (+6 / -4 lines: import + 2 call sites)
    - src/property_lookup.py (+33 / -3 lines: allowlist + post-enrichment Smarty block)
decisions:
  - Keep underscore-prefixed aliases in pre_probate_pipeline_al.py so distress_proxy_pipeline.py and any external callers keep resolving without a follow-up update
  - Function-scoped import for the Smarty helpers in property_lookup.py mirrors the existing pattern for enrich_notice_with_property — keeps the legacy daily flow's startup cost unchanged for non-probate notice types
  - Anchor tuples stay underscore-prefixed (_MADISON_ANCHORS / _MARSHALL_ANCHORS) — internal data, not part of the helper API
metrics:
  duration: ~10 min
  completed: 2026-05-23
---

# Quick Task 260523-uvu: Move Madison + Marshall Smarty ZIP-recovery helpers + wire into legacy daily flow

Mechanical relocation of the three AssuranceWeb Smarty ZIP-recovery helpers from `pre_probate_pipeline_al.py` into the shared `address_standardizer.py` module under public names, plus the actual production gap-close: extending `property_lookup.py` (the legacy `main.py daily` consumer) to allowlist Marshall and to post-enrich Madison + Marshall notices with a Smarty geocode so probate notices with a situs but no ZIP stop getting dropped at the downstream tier filter.

## Tasks completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | Move Smarty helpers + anchor tuples + `_smarty_lookup_once` into `address_standardizer.py` under public names; add backward-compat alias re-export block in `pre_probate_pipeline_al.py` | `bd9ed0d` |
| 2 | Switch `apn_probate_pipeline_al.py` to import the public names from `address_standardizer` (kept `_normalize_decedent_key` / `_promote_heir_contacts_to_csv_slots` import from `pre_probate_pipeline_al`); update 2 call sites; live Smarty smoke-test verified | `a76f904` |
| 3 | Add `"marshall"` to `property_lookup.py` AL-county allowlist; insert post-enrichment Smarty geocode for Madison + Marshall inside the `if matched:` success path (before `await asyncio.sleep(...)` rate-limit) | `07a48fa` |

## Files moved/changed — import-path before/after

### Before

```python
# src/pre_probate_pipeline_al.py owned the helpers (private):
_MADISON_ANCHORS, _MARSHALL_ANCHORS
def _smarty_lookup_once(situs, lastline_hint) -> tuple[str, str]
def _smarty_zip_for_assuranceweb_address(...) -> tuple[str, str]
def _smarty_zip_for_madison_address(situs) -> tuple[str, str]
def _smarty_zip_for_marshall_address(situs) -> tuple[str, str]

# src/apn_probate_pipeline_al.py reached into the sibling pipeline's private namespace:
from pre_probate_pipeline_al import (
    _smarty_zip_for_madison_address,
    _smarty_zip_for_marshall_address,
)

# src/property_lookup.py: Marshall NOT in allowlist, no post-enrichment Smarty
if notice.county.lower() in ("jefferson", "madison"):
    ...
    matched = enrich_notice_with_property(notice)
    if matched: ...  # but Madison notices with no ZIP would get dropped at tier filter
```

### After

```python
# src/address_standardizer.py owns the helpers (public):
def smarty_zip_for_assuranceweb_address(situs, lastline_hint="Huntsville AL", anchor_fallbacks=None) -> tuple[str, str]
def smarty_zip_for_madison_address(situs) -> tuple[str, str]
def smarty_zip_for_marshall_address(situs) -> tuple[str, str]
# (_smarty_lookup_once + anchor tuples are internal implementation detail)

# src/pre_probate_pipeline_al.py keeps underscore-prefixed aliases for backward compat:
from address_standardizer import (
    smarty_zip_for_assuranceweb_address,
    smarty_zip_for_madison_address,
    smarty_zip_for_marshall_address,
)
_smarty_zip_for_assuranceweb_address = smarty_zip_for_assuranceweb_address
_smarty_zip_for_madison_address = smarty_zip_for_madison_address
_smarty_zip_for_marshall_address = smarty_zip_for_marshall_address

# src/apn_probate_pipeline_al.py imports from address_standardizer (public names):
from pre_probate_pipeline_al import (
    _normalize_decedent_key,
    _promote_heir_contacts_to_csv_slots,
)
from address_standardizer import (
    smarty_zip_for_madison_address,
    smarty_zip_for_marshall_address,
)

# src/property_lookup.py: Marshall in allowlist + post-enrichment Smarty for Madison/Marshall
if notice.county.lower() in ("jefferson", "madison", "marshall"):
    ...
    matched = enrich_notice_with_property(notice)
    if matched:
        ... found += 1
        county_lc = notice.county.lower()
        if county_lc in ("madison", "marshall") and notice.address and not notice.zip:
            from address_standardizer import (
                smarty_zip_for_madison_address,
                smarty_zip_for_marshall_address,
            )
            helper = smarty_zip_for_marshall_address if county_lc == "marshall" else smarty_zip_for_madison_address
            city, zip_code = helper(notice.address)
            if zip_code:
                notice.zip = zip_code
                if not notice.city and city:
                    notice.city = city
```

`src/distress_proxy_pipeline.py` is intentionally NOT touched — it imports `_smarty_zip_for_madison_address` / `_smarty_zip_for_marshall_address` from `pre_probate_pipeline_al`, and those names now resolve through the alias re-export block. No behavior change there.

## Live Smarty smoke-test result

`smarty_zip_for_madison_address('100 Church St Sw')` returned:

```
city='Huntsville' zip='35801'
```

Single Smarty US Street API call, sub-second response, ~$0.001 cost. `.env` Smarty creds (rotated 2026-05-23) verified working.

End-to-end verification script (per plan `<verification>` block) exited with `ALL VERIFICATION PASSED` after running all five gates:

1. New public path importable from `address_standardizer` ✓
2. Legacy private path (backward compat) importable from `pre_probate_pipeline_al` with `is`-identity to public names ✓
3. `apn_probate_pipeline_al.smarty_zip_for_madison_address is address_standardizer.smarty_zip_for_madison_address` ✓
4. `property_lookup.py` imports cleanly, contains `marshall`, contains `smarty_zip_for_marshall_address` ✓
5. Live Smarty hit returns a non-empty 5-digit ZIP for the smoke address ✓

## Production behavior change (the actual gap closed)

The helper relocation in Tasks 1 + 2 is mechanical refactor — no behavior change for the post-probate pipelines (`apn_probate_pipeline_al.py`, `pre_probate_pipeline_al.py`, `distress_proxy_pipeline.py`). They already used these helpers and already recovered missing ZIPs.

**Task 3 is the actual production behavior change.** The legacy `main.py daily` flow runs through `property_lookup.py`. Before this commit:

- Marshall County probate notices were dropped entirely — `notice.county.lower() == "marshall"` failed the `("jefferson", "madison")` allowlist gate, so `enrich_notice_with_property()` was never called.
- Madison County probate notices that the AssuranceWeb name-search returned without a ZIP (the majority — AssuranceWeb's name-search response only contains the street, not city or ZIP) made it past the allowlist and the locator filled their `address`, but `notice.zip` stayed empty. Downstream tier filtering then dropped them because the ZIP wasn't in Tier 1 ∪ Tier 2 (empty string isn't in any tier set).

After this commit, the legacy `main.py daily` flow's AL-probate path now matches the behavior of the `apn_probate_pipeline_al` + `pre_probate_pipeline_al` orchestrators: Marshall is allowlisted, and Madison + Marshall notices with a situs but no ZIP get a one-shot Smarty geocode that fills `notice.zip` (and `notice.city` when empty) before downstream tier filtering runs.

## Addresses surfaced as Smarty-unresolvable

None. The single live smoke-test address (`100 Church St Sw` Madison) resolved cleanly on the primary `Huntsville AL` anchor. No fallback anchors were exercised; no failure-path logging was triggered. The failure-path logger line (`Smarty could not resolve ZIP for %s %s (kept address, no zip)`) is in place and will fire on the first real daily run that includes a rural/fringe Madison or Marshall address Smarty can't resolve through all anchor fallbacks.

## Deviations from Plan

None — plan executed exactly as written. Three atomic commits, helpers moved verbatim (no signature changes, full docstring on `smarty_zip_for_assuranceweb_address` preserved), aliases wired with `is`-identity, post-enrichment block placed exactly where the plan specified (inside the `if matched:` success path, before `await asyncio.sleep(...)`).

## Self-Check: PASSED

Files verified to exist:
- `src/address_standardizer.py` — FOUND (contains `smarty_zip_for_assuranceweb_address`, `smarty_zip_for_madison_address`, `smarty_zip_for_marshall_address`, `_smarty_lookup_once`, `_MADISON_ANCHORS`, `_MARSHALL_ANCHORS`)
- `src/pre_probate_pipeline_al.py` — FOUND (contains the alias re-export block)
- `src/apn_probate_pipeline_al.py` — FOUND (imports public helpers from `address_standardizer`; 0 underscore-prefixed `_smarty_zip_for` references; 4 public `smarty_zip_for` references)
- `src/property_lookup.py` — FOUND (allowlist includes `"marshall"`; imports + uses both Smarty helpers)

Commits verified in `git log --oneline -5`:
- `bd9ed0d` — FOUND
- `a76f904` — FOUND
- `07a48fa` — FOUND
