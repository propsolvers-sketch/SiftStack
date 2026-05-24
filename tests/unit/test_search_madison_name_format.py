"""Regression net for BUGFIX-01 — Madison ``_search_madison`` name-format bug.

The original ``_search_madison`` implementation passed ``parts[0]`` as the
``last_name`` for multi-token input, which interpreted the probate-notice
format ``"FIRST MIDDLE LAST"`` as ``last="FIRST", first="MIDDLE LAST"`` and
silently degraded the Madison probate property-locator hit rate to near-zero.
The user only noticed because Madison was returning ~0 hits while Jefferson
(which already had a last-first retry) was returning ~normal volume.

The fix in ``src/probate_property_locator.py:176-232`` now tries BOTH
interpretations of an unhyphenated multi-token name:

* ``A) (parts[-1], parts[:-1])``  — probate-notice format ``FIRST MIDDLE LAST``
* ``B) (parts[0],  parts[1:])``   — assessor / tax-roll format ``LAST FIRST MIDDLE``

…and deduplicates the results by ``parcel_number`` so a property that matches
both interpretations is not double-counted.

See ``.planning/codebase/CONCERNS.md`` ("Madison probate-property locator
passes the wrong name") and ``.planning/REQUIREMENTS.md`` BUGFIX-01.

# ── Why monkeypatch the api module, not the locator module ──────────────
``_search_madison`` does a lazy function-scope import::

    def _search_madison(name: str) -> list[_PropertyRecord]:
        from madison_property_api import search_by_owner_name
        ...

So ``probate_property_locator.search_by_owner_name`` does NOT exist as a
module-level name and ``monkeypatch.setattr("probate_property_locator.search_by_owner_name", ...)``
would either fail or silently do nothing. Patching the attribute on the
``madison_property_api`` module itself is the correct target — the lazy import
re-binds ``search_by_owner_name`` to the patched attribute each call.

Same reasoning applies to the Jefferson sibling guard: patch
``jefferson_property_api.search_by_owner_name``.

All 6 tests run fully offline (no AssuranceWeb / E-Ring HTTP).
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import probate_property_locator as locator


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_rec(parcel: str, owner: str = "SMITH MARY A") -> Any:
    """Lightweight stand-in for ``MadisonPropertyRecord``.

    Duck-typed on ``parcel_number`` (the dedup key in ``_search_madison``)
    and ``owner_name`` (for any downstream consumer). The real
    ``MadisonPropertyRecord`` is a frozen dataclass with ~12 required fields;
    we don't need them for these unit tests.
    """
    return types.SimpleNamespace(parcel_number=parcel, owner_name=owner)


# ── Madison guards (BUGFIX-01 — the primary regression class) ────────────


def test_madison_space_separated_tries_both_interpretations(monkeypatch):
    """The PRIMARY BUGFIX-01 guard.

    Input ``"MARY ANGELA SMITH"`` (probate-notice format) MUST issue:
      * ``search_by_owner_name("SMITH",  "MARY ANGELA")`` — interpretation A
        (drops the bug: previously this query never fired)
      * ``search_by_owner_name("MARY",   "ANGELA SMITH")`` — interpretation B
        (the original buggy behavior — kept as fallback for tax-roll format)

    Both calls must appear; if only one survives a future refactor, the
    Madison hit-rate regression returns silently.
    """
    calls: list[tuple[str, str | None]] = []

    def fake(last, first=None, **kw):
        calls.append((last, first))
        return []

    monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)

    locator._search_madison("MARY ANGELA SMITH")

    assert ("SMITH", "MARY ANGELA") in calls, (
        f"BUGFIX-01 regression: missing FIRST-MIDDLE-LAST interpretation; got {calls}"
    )
    assert ("MARY", "ANGELA SMITH") in calls, (
        f"missing LAST-FIRST-MIDDLE interpretation; got {calls}"
    )


def test_madison_comma_form_uses_single_call(monkeypatch):
    """Comma form is unambiguous (``"LAST, FIRST MIDDLE"``) — no need to try
    both interpretations. Assert exactly one call with the split intact.
    """
    calls: list[tuple[str, str | None]] = []

    def fake(last, first=None, **kw):
        calls.append((last, first))
        return []

    monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)

    locator._search_madison("SMITH, MARY ANGELA")

    assert calls == [("SMITH", "MARY ANGELA")], (
        f"comma form should short-circuit to a single call; got {calls}"
    )


def test_madison_single_token_calls_once(monkeypatch):
    """Single-token input has no first/last ambiguity. Assert one call,
    ``first=None``.
    """
    calls: list[tuple[str, str | None]] = []

    def fake(last, first=None, **kw):
        calls.append((last, first))
        return []

    monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)

    locator._search_madison("SMITH")

    assert calls == [("SMITH", None)], (
        f"single-token should call once with first=None; got {calls}"
    )


def test_madison_empty_returns_empty_list(monkeypatch):
    """Empty input must return ``[]`` without raising and without making any
    API calls (guards against accidentally querying ``last=""`` which would be
    expensive / pointless).
    """
    calls: list[tuple[str, str | None]] = []

    def fake(last, first=None, **kw):
        calls.append((last, first))
        return []

    monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)

    result = locator._search_madison("")

    assert result == [], f"expected [] for empty input, got {result}"
    assert calls == [], f"expected zero API calls for empty input, got {calls}"


def test_madison_dedupes_by_parcel_number(monkeypatch):
    """Both interpretations may legitimately return the SAME parcel — e.g. a
    property recorded as ``"SMITH MARY A"`` matches both ``(SMITH, MARY ANGELA)``
    and ``(MARY, ANGELA SMITH)`` queries via the API's prefix matching. The
    locator must return ONE row, not two.

    Fake returns the same record (same parcel_number) for both calls; assert
    the combined output is deduplicated.
    """
    same_parcel = "14-06-23-4-000-043.000"
    shared_rec = _make_rec(same_parcel, owner="SMITH MARY A")

    def fake(last, first=None, **kw):
        # Both interpretations return the same parcel — dedup must collapse them.
        return [shared_rec]

    monkeypatch.setattr("madison_property_api.search_by_owner_name", fake)

    result = locator._search_madison("MARY ANGELA SMITH")

    assert len(result) == 1, (
        f"expected dedup-by-parcel_number to return 1 record; got {len(result)}: {result}"
    )
    assert result[0].parcel_number == same_parcel


# ── Jefferson sibling guard (REFAC-01 defense) ───────────────────────────


def test_jefferson_still_retries_last_first(monkeypatch):
    """Parallel guard on the sibling adapter ``_search_jefferson``.

    A future "consolidate the 4 county adapters" refactor (REFAC-01, deferred
    to v2) is the most likely vector for silently dropping Jefferson's
    last-first retry while patching Madison. This test pins that the retry
    fires for probate-notice input ``"OPAL W SMITH"``.

    We do NOT assert exact call count — ``_search_jefferson`` has three retry
    layers (original, last-first reorder, truncate-to-LAST+FIRST) and more
    may be added. The regression class is "the last-first reorder retry
    disappears", not "the retry count changes".
    """
    calls: list[str] = []

    def fake(name, **kw):
        calls.append(name)
        return []

    monkeypatch.setattr("jefferson_property_api.search_by_owner_name", fake)

    locator._search_jefferson("OPAL W SMITH")

    # Must include the last-first reorder ("SMITH OPAL W") — the canonical
    # Jefferson tax-roll order. Match leniently (starts with SMITH) so the
    # test doesn't break if the helper adds intermediate whitespace.
    reordered = [c for c in calls if c.strip().upper().startswith("SMITH")]
    assert reordered, (
        "Jefferson regression: last-first reorder query missing. "
        f"Expected at least one call starting with 'SMITH'; got {calls}"
    )
