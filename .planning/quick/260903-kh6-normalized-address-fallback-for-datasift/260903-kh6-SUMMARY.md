---
phase: quick-260903-kh6
plan: 01
subsystem: datasift-integration
tags: [datasift, address-matching, normalization, regression-suite]
requires:
  - src/datasift_api.py
  - src/al_property_enricher.py
provides:
  - src/street_suffixes.py (SUFFIX_ABBR, canonical_street)
  - datasift_api._property_index_key_normalized
  - datasift_api._ingest_property_records
  - datasift_api._property_index_normalized
  - datasift_api._property_index_ambiguous
affects:
  - scripts/apply_subtype_tags.py
  - scripts/apply_courthouse_snapshots.py
  - scripts/capture_today_probate_uuids.py
  - scripts/capture_today_standard_uuids.py
tech-stack:
  added: []
  patterns:
    - "Exact-first / normalized-on-miss two-tier index lookup"
    - "Ambiguity guard: distinct UUIDs on one normalized key return None rather than guess"
    - "Greppable log token (DS_ADDR_FALLBACK) for counting fallback hits in GHA logs"
key-files:
  created:
    - src/street_suffixes.py
    - tests/test_datasift_address_index.py
  modified:
    - src/datasift_api.py
    - src/al_property_enricher.py
decisions:
  - "Suffix map moved to a zero-dependency module so datasift_api can use it without importing notice_parser"
  - "Normalization collapses BOTH sides to the abbreviated form (DataSift's storage form), not expansion"
  - "Ambiguous normalized keys return None — a wrong tag writes durable CRM state, a missing tag is recoverable"
  - "owner_cache._SUFFIX_MAP left alone: different semantics (expands directionals), consolidating it is out of scope"
metrics:
  duration: ~18 min
  tasks: 3
  files: 4
  commits: 4
  completed: 2026-09-03
---

# Quick 260903-kh6: Normalized Address Fallback for DataSift Summary

Street-suffix-tolerant DataSift address lookup: an exact-first index with a
`canonical_street()`-normalized fallback and a collision guard that returns `None`
rather than tagging the wrong property.

## What Was Built

`datasift_api.find_property_uuid_by_address()` was an exact lowercase string match, so
`807 Briarwood Drive` (T&B trustee portal) never matched `807 Briarwood Dr` (DataSift).
GHA run 33728416332 shows the live failure — the `foreclosure_cancelled` subtype tag
never landed, and the same silent degradation hit all four callers of this lookup.

Three pieces:

1. **`src/street_suffixes.py`** — the 20-entry `SUFFIX_ABBR` map moved verbatim out of
   `al_property_enricher.py`, plus a pure `canonical_street()` normalizer. Zero project
   and third-party imports, so `datasift_api` (stdlib + `requests` only) can depend on
   it without pulling in `notice_parser` or the smartystreets SDK.
   `al_property_enricher` keeps its `_SUFFIX_ABBR` private alias, so
   `_address_search_variants` is bit-identical.

2. **`src/datasift_api.py`** — a second `_property_index_normalized` dict and a
   `_property_index_ambiguous` set, both built in the SAME pagination pass (no extra
   `_get` call). Per-record ingestion factored into the network-free
   `_ingest_property_records()` seam. `find_property_uuid_by_address` tries the exact
   key first and returns immediately on a hit; only on a miss does it consult the
   normalized index, and it returns `None` when the normalized key is ambiguous.

3. **`tests/test_datasift_address_index.py`** — 24 offline checks in the repo's
   `check()`-runner convention. No network, no `DATASIFT_API_KEY`.

## Tasks Completed

| Task | Name | Commit |
| ---- | ---- | ------ |
| 1 | Extract street-suffix map into shared zero-dependency module | `7aa4d9f` |
| 2 (RED) | Failing tests for normalized fallback + ambiguity guard | `d949dab` |
| 2 (GREEN) | Normalized fallback index, ambiguity guard, fallback logging | `d5afd53` |
| 3 | Full offline regression suite | `1d0d6ac` |

## Key Decisions

**Collapse to the abbreviated form, not expansion.** DataSift and the AL county
assessors both store `DR`, not `Drive`. Normalizing toward the abbreviation means an
already-abbreviated string passes through unchanged (apart from case/whitespace), which
is exactly what makes both spelling directions compare equal with one transform.

**Ambiguity returns None, never a guess.** If two distinct UUIDs collapse onto one
normalized key, the key is deleted from the normalized index and added to the ambiguous
set, with one `logger.warning` naming the key and both UUIDs (emitted only on the
transition, so it stays one line per key). Rationale: tagging the wrong property writes
durable state into the operator's CRM; tagging nothing is recoverable.

**Exact path untouched.** `_property_index_key()` is byte-identical and is still tried
first with an immediate return. Lookups that succeed today pay zero extra cost and
cannot be displaced by a normalized collision — pinned by a test that makes the
normalized key ambiguous while the exact key is present.

**Conservative normalization.** Only the trailing suffix token (or the second-to-last
token when a directional trails it, e.g. `DRIVE SW`) is rewritten, and only against the
existing 20-entry map. House number, street name, and zip5 are never altered, and zip5
handling is byte-identical to the exact key, so cross-ZIP matches remain impossible.

## Verification

| Check | Result |
| ----- | ------ |
| `tests/test_datasift_address_index.py` | 24 passed, 0 failed, exit 0 |
| `tests/test_entity_researcher.py` (no regression from the extraction) | 55 passed, 0 failed, exit 0 |
| Files changed since base | exactly the 4 planned files; nothing under `scripts/` |
| `_get(` call sites inside `build_property_index` | exactly 1 — no duplicated pagination |
| `_property_index` body in diff | unmodified (no removed lines in that function) |
| `DS_ADDR_FALLBACK` log line | confirmed emitted on a normalized hit, silent on an exact hit |

Manual log check output:

```
INFO DS_ADDR_FALLBACK matched '807 Briarwood Drive' (35022) via normalized key '807 briarwood dr|35022' → UUID-BRIAR
```

The originally reported case now resolves in both directions: an index built from
`807 Briarwood Dr` answers a `807 Briarwood Drive` query with `UUID-BRIAR`, and vice
versa.

## Deviations from Plan

**1. Verification commands re-pointed at the worktree**

- **Found during:** Task 1
- **Issue:** The plan's `<automated>` verify blocks hardcode
  `sys.path.insert(0, '/Users/shanismith/Desktop/SiftStack/src')` — the MAIN repo — and
  invoke bare `python`, which is not on PATH non-interactively. Run as written they
  would have validated the unmodified main-repo source, not this worktree's edits.
- **Fix:** Ran the identical assertions via the main repo's `.venv/bin/python` with the
  worktree's `src/` on `sys.path`, and printed `module.__file__` on every run to prove
  the worktree copy was under test. Assertion content unchanged. The committed test
  file uses a `__file__`-relative `sys.path.insert`, so it resolves correctly wherever
  the worktree lives.
- **Files modified:** none (verification harness only)

**2. [Rule 3] Test file created during Task 2 rather than only in Task 3**

- **Found during:** Task 2
- **Issue:** Task 2 is marked `tdd="true"`, which requires a failing test committed
  before implementation, but the plan places the test file in Task 3.
- **Fix:** Created `tests/test_datasift_address_index.py` in the RED gate covering
  Task 2's six `<behavior>` bullets (11 checks, confirmed failing on
  `AttributeError: _property_index_key_normalized`), then implemented and committed
  GREEN. Task 3 expanded the same file to the full required section list (24 checks).
  Net artifact matches the plan exactly; only the commit sequencing differs.
- **Files modified:** `tests/test_datasift_address_index.py`

## Deferred / Out of Scope

**`src/owner_cache.py:_SUFFIX_MAP` is a second, independent suffix table.** Task 1's
done-criteria expected `grep '"DRIVE": "DR"' src/*.py` to match exactly one file; it
matches two. `owner_cache._SUFFIX_MAP` is pre-existing (not introduced here) and has
different semantics — it also maps directionals (`NORTH` → `N`, `SOUTHWEST` → `SW`) and
carries `AV` → `AVE` and `SQUARE` → `SQ`, which `SUFFIX_ABBR` does not. Folding it into
`street_suffixes` would change `owner_cache._normalize_key()` output and invalidate the
existing `foreclosure_owner_cache.json` keys. Left untouched deliberately; consolidation
would need its own task with a cache-migration step.

## Known Stubs

None.

## Threat Flags

None — no new network endpoint, auth path, file access pattern, or schema change was
introduced. The change is confined to in-memory index construction and lookup.

## TDD Gate Compliance

RED (`d949dab`, `test(...)`) → GREEN (`d5afd53`, `feat(...)`). No REFACTOR commit was
needed; the GREEN implementation required no cleanup pass.

## Self-Check: PASSED

All 4 claimed files exist on disk (`src/street_suffixes.py`,
`tests/test_datasift_address_index.py`, `src/datasift_api.py`,
`src/al_property_enricher.py`) and all 4 claimed commit hashes (`7aa4d9f`, `d949dab`,
`d5afd53`, `1d0d6ac`) resolve in `git log`. Working tree is clean.
