"""Regression net for BUGFIX-03 — ``_GARBAGE_RE`` shape vs the docstring intent.

The original regex in ``src/enrichment_pipeline.py`` was
``re.compile(r"^[^a-zA-Z0-9]*$")`` — a character class that included BOTH
letters AND digits in the negation. The pattern therefore only matched
strings that were ENTIRELY non-alphanumeric (e.g. ``"---"`` or ``""``) and
silently passed numeric-only OCR garbage like ``"12345"`` (a parcel number
leaked into the address slot from upstream extraction) on through validation
into the DataSift CSV. Postcards then mailed to invalid addresses; the
operator only noticed weeks later when return-to-senders piled up.

The fix is ``_GARBAGE_RE = re.compile(r"^[^a-zA-Z]*$")`` — drops ``0-9`` from
the negation so the regex matches any string with **zero letters**. Numeric-
only addresses, punctuation-only addresses, whitespace-only, and the empty
string all match (= garbage); anything containing at least one letter does
not match (= keep). The comment block at
``src/enrichment_pipeline.py:228-234`` documents the bug class.

See ``.planning/codebase/CONCERNS.md`` ("``_GARBAGE_RE`` doesn't do what the
docstring says") and ``.planning/REQUIREMENTS.md`` BUGFIX-03.

# ── Why this test file exists ───────────────────────────────────────────
The fix is a one-character change (drop ``0-9`` from the character class)
and is therefore trivially easy to revert by accident during a refactor.
A future maintainer "consolidating" or "tightening" the validator could:

  1. Revert the regex back to ``r"^[^a-zA-Z0-9]*$"`` (the original bug).
  2. "Fix" the regex by re-introducing ``\\d`` into the character class
     (would re-introduce the bug class with different syntax).
  3. Silently bypass the check by removing the ``_GARBAGE_RE.match()`` call
     from ``_validate_records`` during a refactor.

Numeric-only addresses passing validation is a low-frequency but persistent
quality leak. This test pins the regex shape AND the ``_validate_records``
integration path so any of the three regression modes above fails loudly.

All 10 tests run fully offline. ``_validate_records`` is a pure function over
an in-memory ``list[NoticeData]`` — no monkeypatching needed, no HTTP, no
external services. Test #10 (owner_street garbage check on probate) is
intentionally skipped — the current validator does NOT apply
``_GARBAGE_RE`` to ``owner_street``; symmetric owner_street validation is
a separate hardening ticket, NOT in BUGFIX-03 scope.
"""

from __future__ import annotations

import pytest

from enrichment_pipeline import _GARBAGE_RE, _validate_records
from notice_parser import NoticeData


# ── Test helpers ────────────────────────────────────────────────────────


def _make_notice(**overrides) -> NoticeData:
    """Build a NoticeData with sensible non-probate defaults.

    Every required-for-non-probate field is populated with a plausible value
    so that the only thing under test is whatever the caller overrides.
    Following the NoticeData convention (CONVENTIONS.md "Empty strings,
    never None") — all defaults are empty strings on the dataclass itself;
    we only set what the validator reads.
    """
    defaults: dict[str, str] = dict(
        notice_type="foreclosure",
        address="5100 Stokely Lane",
        city="Birmingham",
        state="AL",
        zip="35203",
        date_added="2026-05-01",
        received_date="2026-05-01",
    )
    defaults.update(overrides)
    n = NoticeData()
    for k, v in defaults.items():
        setattr(n, k, v)
    return n


# ── Unit: _GARBAGE_RE shape ─────────────────────────────────────────────
# Six assertions pin the post-fix regex behavior. Reverting the regex to
# r"^[^a-zA-Z0-9]*$" makes test_garbage_re_matches_numeric_only fail
# immediately — that is the canonical BUGFIX-03 case.


def test_garbage_re_matches_numeric_only():
    """A purely numeric address is garbage (the canonical BUGFIX-03 case).

    Upstream OCR / regex extraction occasionally leaks a parcel number or
    a docket number into ``notice.address``. The original regex
    ``r"^[^a-zA-Z0-9]*$"`` did NOT match ``"12345"`` because digits were
    excluded from the negation; the record passed validation and shipped
    to DataSift as unmailable noise. The fix matches it as garbage.
    """
    assert _GARBAGE_RE.match("12345")


def test_garbage_re_matches_punctuation_only():
    """Pure punctuation is garbage — both the original and fixed regex agree."""
    assert _GARBAGE_RE.match("---")


def test_garbage_re_matches_empty():
    """Empty string is garbage — both the original and fixed regex agree."""
    assert _GARBAGE_RE.match("")


def test_garbage_re_matches_whitespace_only():
    """Whitespace-only is garbage — both the original and fixed regex agree."""
    assert _GARBAGE_RE.match("   ")


def test_garbage_re_does_not_match_real_address():
    """A real street address contains letters → NOT garbage.

    Uses the canonical fixture address shared with ``tests/test_parser.py``
    so the negative test is anchored on a known-real value.
    """
    assert not _GARBAGE_RE.match("5100 Stokely Lane")


def test_garbage_re_does_not_match_letter_only():
    """Pure-letter string contains letters → NOT garbage.

    The regex is the docstring-intent "at least one letter required",
    not "must look like a full address" — that's a different concern.
    """
    assert not _GARBAGE_RE.match("Main St")


# ── Integration: _validate_records on NoticeData ────────────────────────
# Three assertions pin the validator's end-to-end behavior on real
# NoticeData fixtures. test_validator_drops_numeric_only_address is the
# production-behavior regression test for the case BUGFIX-03 fixes.


def test_validator_drops_numeric_only_address():
    """A foreclosure record with a numeric-only ``address`` is dropped.

    This is the production-behavior regression test. Same input as
    test_garbage_re_matches_numeric_only, but exercises the full
    ``_validate_records`` path — guards against the third regression mode
    (someone removing the ``_GARBAGE_RE.match()`` call from the validator
    during a refactor).
    """
    notice = _make_notice(address="12345")
    result = _validate_records([notice])
    assert result == [], (
        "Numeric-only address must be dropped — BUGFIX-03 regression. "
        "Either _GARBAGE_RE shape changed or the validator stopped calling it."
    )


def test_validator_keeps_letter_containing_address():
    """A foreclosure record with a real, letter-containing address is kept.

    Anchors the positive case so a future over-aggressive regex (e.g.
    accidentally matching anything containing a digit) is caught — the
    fix must remain ASYMMETRIC about letters, not digits.
    """
    notice = _make_notice(address="5100 Stokely Lane")
    result = _validate_records([notice])
    assert len(result) == 1
    assert result[0].address == "5100 Stokely Lane"


def test_validator_keeps_probate_with_pr_mailing():
    """Probate exemption survives the BUGFIX-03 fix.

    The validator's ``_NO_PROPERTY_ADDRESS_TYPES = {"probate", "divorce"}``
    carve-out lets probate records pass when a PR mailing address (or
    decision-maker mailing address) is populated, even if the property
    ``address`` is empty (locator missed the parcel). The fix to
    ``_GARBAGE_RE`` is orthogonal to this carve-out — verifies the
    exemption was not collateral damage. ``owner_name`` is required for
    probate (``_validate_records`` line ~281).
    """
    notice = _make_notice(
        notice_type="probate",
        address="",
        city="",
        zip="",
        owner_name="Jane Doe, Personal Representative",
        owner_street="123 Main St",
        owner_city="Birmingham",
        owner_state="AL",
        owner_zip="35203",
    )
    result = _validate_records([notice])
    assert len(result) == 1, (
        "Probate exemption regressed — BUGFIX-03 fix should not touch "
        "the _NO_PROPERTY_ADDRESS_TYPES carve-out."
    )


@pytest.mark.skip(
    reason=(
        "Future hardening — not in current validator scope. "
        "_validate_records does not currently call _GARBAGE_RE on owner_street, "
        "only on the property address. A symmetric check on the PR mailing path "
        "would catch garbage in the owner_street slot too. See "
        "01-bugfix-03-SUMMARY.md 'Known gaps / follow-up' for the recommended "
        "follow-up ticket."
    )
)
def test_validator_drops_probate_with_garbage_owner_street():
    """Probate with numeric-only ``owner_street`` should be dropped.

    Skipped: the current validator only applies ``_GARBAGE_RE`` to
    ``n.address``, not to ``n.owner_street``. To make this pass we would
    have to modify production code — out of scope for BUGFIX-03. Left as a
    documented skip so the gap is visible in pytest output and the test
    can be flipped on by a future hardening ticket.
    """
    notice = _make_notice(
        notice_type="probate",
        address="",
        city="",
        zip="",
        owner_name="Jane Doe, Personal Representative",
        owner_street="99999",
        owner_city="Birmingham",
        owner_state="AL",
        owner_zip="35203",
    )
    result = _validate_records([notice])
    assert result == [], "PR mailing path should also reject numeric-only owner_street."
