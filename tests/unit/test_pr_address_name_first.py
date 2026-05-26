"""Regression net for PARSER-01 — ``PR_ADDRESS_NAME_FIRST_RE`` + the
``_parse_pr_address()`` OR-chain that wires it into the probate pipeline.

Alabama probate Notice-to-Creditors publications use a vertical signature
block where the PR's NAME appears BEFORE the title, with only a newline
between the title and the mailing address:

    JOHN SMITH
    Personal Representative
    123 Main St
    Birmingham, AL 35203

The legacy ``PR_ADDRESS_RE`` (Tennessee inline format — ``"Personal
Representative: NAME, ADDR"``) requires ``[^0-9]{3,80}`` non-digit chars
between the title keyword and the first digit of the house number, so it
NEVER matches the AL layout (just a newline = 1 char). Result: every AL
probate notice shipped without an ``owner_*`` PR mailing address unless the
LLM fallback was online (``ANTHROPIC_API_KEY`` set, Haiku not rate-limited,
not cost-throttled). The fix at ``src/notice_parser.py:635`` adds
``PR_ADDRESS_NAME_FIRST_RE`` with ``[^0-9]{0,80}?`` (zero minimum,
non-greedy) so the address can follow the title with minimal whitespace, and
``_parse_pr_address`` at line 1982 chains the two regexes
(``PR_ADDRESS_RE.search(text) or PR_ADDRESS_NAME_FIRST_RE.search(text)``)
so both TN inline AND AL signature-block formats parse.

See:
  - ``.planning/codebase/CONCERNS.md`` "``PR_ADDRESS_RE`` is name-after-title
    only"
  - ``CLAUDE.md`` "AL probate PR-name extraction (multi-pattern)" — documents
    the same bug class for the related ``_parse_name`` regex chain
  - ``.planning/REQUIREMENTS.md`` PARSER-01

# ── Why this test file exists ───────────────────────────────────────────
The fix is additive (a second regex + an ``or`` clause). A future
maintainer "consolidating" the two regexes into one — or dropping the
``or`` clause during a refactor — would silently re-introduce the original
bug class: AL probate records ship without PR mailing addresses unless the
LLM is paying the toll. This test file pins:

  1. ``PR_ADDRESS_NAME_FIRST_RE`` matches all 5 AL-recognized fiduciary
     titles (Personal Representative, Executor, Executrix, Administrator,
     Administratrix per Ala. Code § 43-2).
  2. ``PR_ADDRESS_RE`` (the TN inline original) still matches the TN inline
     format — the fix is provably additive, NOT a replacement that would
     break the legacy Knox/Blount photo-import path.
  3. ``_parse_pr_address`` chains BOTH regexes via the ``or`` clause and
     populates ``owner_street`` / ``owner_city`` / ``owner_state`` /
     ``owner_zip`` on an AL signature-block fixture end-to-end.
  4. ``_parse_pr_address`` is a no-op when ``notice_type != "probate"`` — a
     foreclosure / tax / code-violation record cannot have its ``owner_*``
     slots silently corrupted by stray PR-address text in raw_text.
  5. ALL-CAPS street addresses (common in newspaper-published notices) get
     title-cased per the ``.isupper() -> .title()`` branch at
     ``notice_parser.py:1986``.
  6. ``owner_state`` defaults to ``"AL"`` when ``notice.state`` is empty
     (``notice.state or "AL"`` fallback at line 1992) so AL probates without
     an explicit state assignment still get the right CSV output.

All tests run fully offline. Fixtures are literal Python strings shaped
after real AL signature blocks (per ``TESTING.md`` "paste the real captured
upstream output ... as a string literal").
"""

from __future__ import annotations

import pytest

from notice_parser import (
    PR_ADDRESS_NAME_FIRST_RE,
    PR_ADDRESS_RE,
    _parse_pr_address,
    NoticeData,
)


# ── Fixtures ────────────────────────────────────────────────────────────
# Real-shape AL probate signature block (parameterized in the title slot).
# Captured prose mirrors the Ala. Code § 43-2-61 publication template:
# "Letters Testamentary having been granted on the Estate of NAME, deceased,
# on the Xth day of MONTH, YEAR, by the Honorable JUDGE, Judge of Probate of
# COUNTY County, Alabama, notice is hereby given..."
#
# The "{title}" placeholder gets filled in by the parametrize decorator with
# each of the 5 AL-recognized fiduciary titles. The address shape (123 Main
# St / Birmingham / AL / 35203) stays constant so the same assertion runs
# against every title variant.
AL_SIGNATURE_BLOCK_TEMPLATE = """NOTICE TO CREDITORS

Letters Testamentary having been granted on the Estate of MARY ANGELA SMITH,
deceased, on the 15th day of April, 2026, by the Honorable Allwin Horn,
Judge of Probate of Jefferson County, Alabama, notice is hereby given that all
persons having claims against said estate must file the same within six months
from the date of grant of letters or be barred.

JOHN SMITH
{title}
123 Main St
Birmingham, AL 35203
"""

# Mixed-case canonical AL signature block (no title-case branch exercised).
AL_SIGNATURE_PR = AL_SIGNATURE_BLOCK_TEMPLATE.format(title="Personal Representative")

# ALL-CAPS street fixture — exercises the .isupper() -> .title() branch
# at notice_parser.py:1986 that newspaper-published notices (uppercase
# OCR'd text) frequently trigger.
AL_SIGNATURE_PR_UPPERCASE_STREET = """NOTICE TO CREDITORS

Letters Testamentary having been granted on the Estate of MARY ANGELA SMITH,
deceased, on the 15th day of April, 2026, by the Honorable Allwin Horn,
Judge of Probate of Jefferson County, Alabama, notice is hereby given that all
persons having claims against said estate must file the same within six months
from the date of grant of letters or be barred.

JOHN SMITH
Personal Representative
123 MAIN STREET
BIRMINGHAM, AL 35203
"""

# Legacy TN inline format — the original PR_ADDRESS_RE shape. Kept here so
# the regression guard for the "fix is additive, not a replacement" truth
# has a single source of truth.
TN_INLINE_PR_TEXT = (
    "Personal Representative: Jane Doe, 789 Pine St, Knoxville, TN 37918"
)

# Parameterized title set — Ala. Code § 43-2 grants Letters of any of these
# titles. The signature block keeps the same vertical layout regardless.
AL_PR_TITLES = [
    "Personal Representative",
    "Executor",
    "Executrix",
    "Administrator",
    "Administratrix",
]


def _make_probate_notice(raw_text: str, *, state: str = "AL") -> NoticeData:
    """Build a NoticeData wired for _parse_pr_address() to act on it.

    Keyword-only ``state`` (per CONVENTIONS.md) so callers must be explicit
    when overriding to ``""`` to exercise the AL default-state branch.
    """
    n = NoticeData()
    n.notice_type = "probate"
    n.raw_text = raw_text
    n.state = state
    return n


# ── Unit: PR_ADDRESS_NAME_FIRST_RE ──────────────────────────────────────


@pytest.mark.parametrize("title", AL_PR_TITLES)
def test_name_first_re_matches_all_al_titles(title: str) -> None:
    """PR_ADDRESS_NAME_FIRST_RE matches the AL signature-block layout for
    each of the 5 AL-recognized fiduciary titles (Personal Representative,
    Executor, Executrix, Administrator, Administratrix per Ala. Code § 43-2).

    Pins: truth #1 + truth #2.
    """
    text = AL_SIGNATURE_BLOCK_TEMPLATE.format(title=title)
    match = PR_ADDRESS_NAME_FIRST_RE.search(text)
    assert match is not None, (
        f"PR_ADDRESS_NAME_FIRST_RE should match the AL signature block with "
        f"title {title!r}; got no match. This indicates the regex was "
        f"narrowed or the title alternation in the title-keyword group at "
        f"notice_parser.py:636 was reduced."
    )
    street, city, zip_code = match.groups()
    assert street == "123 Main St"
    assert city == "Birmingham"
    assert zip_code == "35203"


def test_name_first_re_captures_uppercase_street_unchanged() -> None:
    """The regex itself preserves whatever case the source text used — the
    title-casing happens in _parse_pr_address (downstream), NOT in the
    regex. This pins that the capture group returns the raw matched
    substring so the downstream title-case branch has something to work on.
    """
    match = PR_ADDRESS_NAME_FIRST_RE.search(AL_SIGNATURE_PR_UPPERCASE_STREET)
    assert match is not None
    street, city, zip_code = match.groups()
    assert street == "123 MAIN STREET"
    assert city == "BIRMINGHAM"
    assert zip_code == "35203"


# ── Legacy: PR_ADDRESS_RE ───────────────────────────────────────────────


def test_legacy_pr_address_re_still_matches_tn_inline() -> None:
    """The legacy TN inline format still parses through PR_ADDRESS_RE.

    Pins: truth #3 — the fix is provably ADDITIVE. If a future maintainer
    "consolidates" the two regexes into one and accidentally tightens the
    {3,80} requirement, this test fails and surfaces the TN regression
    BEFORE the photo-import path silently drops PR addresses.
    """
    match = PR_ADDRESS_RE.search(TN_INLINE_PR_TEXT)
    assert match is not None, (
        "PR_ADDRESS_RE must still match the legacy TN inline format "
        "'Personal Representative: NAME, ADDR'. If this assertion fails, the "
        "Knox/Blount-era photo-import path has silently regressed."
    )
    street, city, zip_code = match.groups()
    assert street == "789 Pine St"
    assert city == "Knoxville"
    assert zip_code == "37918"


# ── Integration: _parse_pr_address ──────────────────────────────────────


@pytest.mark.parametrize("title", AL_PR_TITLES)
def test_parse_pr_address_populates_owner_fields_for_each_al_title(
    title: str,
) -> None:
    """End-to-end: _parse_pr_address(notice) takes an AL probate notice
    containing a vertical signature block and populates all four owner_*
    fields via the OR-chain fallback to PR_ADDRESS_NAME_FIRST_RE.

    Pins: truth #4 — the OR-chain at notice_parser.py:1982 is the
    integration point that wires the new regex into the probate pipeline.
    Removing the ``or PR_ADDRESS_NAME_FIRST_RE.search(text)`` clause
    causes ALL 5 of these parameterized tests to fail (the TN PR_ADDRESS_RE
    alone cannot match the AL layout).
    """
    text = AL_SIGNATURE_BLOCK_TEMPLATE.format(title=title)
    notice = _make_probate_notice(text, state="AL")

    _parse_pr_address(notice)

    assert notice.owner_street == "123 Main St"
    assert notice.owner_city == "Birmingham"
    assert notice.owner_state == "AL"
    assert notice.owner_zip == "35203"


def test_parse_pr_address_no_op_for_non_probate() -> None:
    """_parse_pr_address must early-return when notice_type != "probate".

    Pins: truth #6 — a foreclosure / tax / code-violation record CANNOT
    have its owner_* slots silently corrupted by stray PR-address text in
    raw_text. The early-return at notice_parser.py:1978-1979 is the guard;
    this test fails if a future refactor removes it.
    """
    notice = _make_probate_notice(AL_SIGNATURE_PR, state="AL")
    notice.notice_type = "foreclosure"

    _parse_pr_address(notice)

    assert notice.owner_street == ""
    assert notice.owner_city == ""
    assert notice.owner_zip == ""


def test_parse_pr_address_title_cases_uppercase_street() -> None:
    """ALL-CAPS street addresses (common in newspaper-published notices
    where the publishing layout is uppercase, or in OCR output) get
    title-cased on assignment via the .isupper() -> .title() branch at
    notice_parser.py:1986.

    Pins: integration of the title-case branch — separate from the regex
    capture-group preservation tested above.
    """
    notice = _make_probate_notice(AL_SIGNATURE_PR_UPPERCASE_STREET, state="AL")

    _parse_pr_address(notice)

    # Was "123 MAIN STREET" in raw_text; title() yields "123 Main Street".
    assert notice.owner_street == "123 Main Street"
    # City goes through _clean_city (not the .isupper branch); raw uppercase
    # comes in but the cleaner normalizes it to title case as well.
    assert notice.owner_city == "Birmingham"
    assert notice.owner_state == "AL"
    assert notice.owner_zip == "35203"


def test_parse_pr_address_defaults_state_to_al_when_notice_state_empty() -> None:
    """owner_state falls back to "AL" when notice.state is empty.

    Pins: truth #5 — the ``notice.state or "AL"`` fallback at
    notice_parser.py:1992. AL probates whose state was not explicitly set
    at scrape time still get the correct CSV output (the legacy default
    when all active pipelines are Alabama).
    """
    notice = _make_probate_notice(AL_SIGNATURE_PR, state="")

    _parse_pr_address(notice)

    assert notice.owner_state == "AL"
    # And the other owner_* fields still populate.
    assert notice.owner_street == "123 Main St"
    assert notice.owner_city == "Birmingham"
    assert notice.owner_zip == "35203"


def test_parse_pr_address_respects_explicit_notice_state() -> None:
    """When notice.state is set (e.g. "TN" from a Knox/Blount-era photo
    import), _parse_pr_address respects it rather than overwriting with "AL".

    Pins: the ``notice.state or "AL"`` order — ``notice.state`` is the
    primary, "AL" is only the fallback. If a refactor flips the order
    (``"AL" or notice.state`` — always wins because "AL" is truthy), this
    test fails.
    """
    # Use the TN inline format so PR_ADDRESS_RE matches (the upstream regex
    # in the OR chain) — proves the state-respect branch is independent of
    # which regex actually fired.
    notice = _make_probate_notice(TN_INLINE_PR_TEXT, state="TN")

    _parse_pr_address(notice)

    assert notice.owner_state == "TN"
    assert notice.owner_street == "789 Pine St"
    assert notice.owner_city == "Knoxville"
    assert notice.owner_zip == "37918"
