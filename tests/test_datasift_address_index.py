"""Unit tests for the DataSift address→UUID index normalized fallback.

Covers the normalized-key fallback, the ambiguity guard, and the untouched
exact fast path in datasift_api. No API calls — every fixture is a synthetic
record dict shaped like the live /property/ response.

Usage:
    python tests/test_datasift_address_index.py
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api

# ── Test Runner ─────────────────────────────────────────────────────────

passed = 0
failed = 0
errors = []


def check(name, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
    else:
        failed += 1
        errors.append(f"FAIL: {name}\n  expected: {expected!r}\n  got:      {actual!r}")


def install_index(rows):
    """Build the three index containers from (street, zip5, uuid) tuples.

    Installs them onto the datasift_api module globals. Because
    _property_index is left NON-None, find_property_uuid_by_address never
    triggers build_property_index() — so these tests make zero HTTP calls.
    Do NOT "helpfully" add a network mock here; there is nothing to mock.
    """
    exact, normalized, ambiguous = {}, {}, set()
    records = [
        {"address": {"street": s, "zip5": z}, "uuid": u} for s, z, u in rows
    ]
    datasift_api._ingest_property_records(records, exact, normalized, ambiguous)
    datasift_api._property_index = exact
    datasift_api._property_index_normalized = normalized
    datasift_api._property_index_ambiguous = ambiguous
    return exact, normalized, ambiguous


def reset_index():
    datasift_api._property_index = None
    datasift_api._property_index_normalized = None
    datasift_api._property_index_ambiguous = None


# ── Normalized Key ──────────────────────────────────────────────────────

print("=== Normalized Key ===")

check(
    "Spelled-out suffix normalizes to abbreviated key",
    datasift_api._property_index_key_normalized("807 Briarwood Drive", "35022"),
    "807 briarwood dr|35022",
)
check(
    "Abbreviated suffix produces the identical key",
    datasift_api._property_index_key_normalized("807 Briarwood Dr", "35022"),
    datasift_api._property_index_key_normalized("807 Briarwood Drive", "35022"),
)
check(
    "zip5 is truncated to 5 chars like the exact key",
    datasift_api._property_index_key_normalized("807 Briarwood Dr", "35022-1234"),
    "807 briarwood dr|35022",
)

# ── Ambiguity Guard ─────────────────────────────────────────────────────

print("=== Ambiguity Guard ===")

reset_index()
_, normalized, ambiguous = install_index([
    ("807 Briarwood Drive", "35022", "UUID-BRIAR"),
    ("807 Briarwood Dr", "35022", "UUID-BRIAR"),
])
check(
    "Same uuid under two spellings is NOT ambiguous",
    "807 briarwood dr|35022" in ambiguous,
    False,
)
check(
    "Same uuid under two spellings stays in the normalized index",
    normalized.get("807 briarwood dr|35022"),
    "UUID-BRIAR",
)

reset_index()
_, normalized, ambiguous = install_index([
    ("100 Oak Street", "35022", "UUID-A"),
    ("100 Oak St", "35022", "UUID-B"),
])
check(
    "Distinct uuids colliding on one normalized key are marked ambiguous",
    "100 oak st|35022" in ambiguous,
    True,
)
check(
    "Ambiguous key is removed from the normalized index",
    "100 oak st|35022" in normalized,
    False,
)

# ── Exact Fast Path ─────────────────────────────────────────────────────

print("=== Exact Fast Path ===")

reset_index()
install_index([
    ("100 Oak Street", "35022", "UUID-A"),
    ("100 Oak St", "35022", "UUID-B"),
])
check(
    "Exact hit wins even when the normalized key is ambiguous",
    datasift_api.find_property_uuid_by_address("100 Oak Street", "35022"),
    "UUID-A",
)
check(
    "Other exact spelling also resolves to its own uuid",
    datasift_api.find_property_uuid_by_address("100 Oak St", "35022"),
    "UUID-B",
)

# ── Ambiguous Miss ──────────────────────────────────────────────────────

print("=== Ambiguous Miss ===")

reset_index()
exact, _, _ = install_index([
    ("100 Oak Street", "35022", "UUID-A"),
    ("100 Oak St", "35022", "UUID-B"),
])
# Drop the exact entry so the lookup is forced onto the ambiguous key.
del exact["100 oak street|35022"]
check(
    "Exact miss + ambiguous normalized key returns None (never guesses)",
    datasift_api.find_property_uuid_by_address("100 Oak Street", "35022"),
    None,
)

# ── Cache Lifecycle (no HTTP) ───────────────────────────────────────────

print("=== Cache Lifecycle ===")

reset_index()
install_index([("807 Briarwood Dr", "35022", "UUID-BRIAR")])


def _explode(*args, **kwargs):
    raise AssertionError("find_property_uuid_by_address made an HTTP call")


_saved_get = datasift_api._get
datasift_api._get = _explode
try:
    check(
        "Populated index performs zero HTTP calls",
        datasift_api.find_property_uuid_by_address("807 Briarwood Drive", "35022"),
        "UUID-BRIAR",
    )
except AssertionError as exc:
    failed += 1
    errors.append(f"FAIL: Populated index performs zero HTTP calls\n  {exc}")
finally:
    datasift_api._get = _saved_get


# ── Summary ─────────────────────────────────────────────────────────────

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
if errors:
    print()
    for e in errors:
        print(e)
    sys.exit(1)
else:
    print("All tests passed!")
