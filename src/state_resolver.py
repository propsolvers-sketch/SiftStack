"""State resolution + validation — the single source of truth for `state`.

Two distinct concerns:

1. PROPERTY state — where the house physically is. Determined by the county
   the notice was scraped from. Jefferson AL → property is in AL. Always.
   Use `state_for_county()`. This is the authority; reject anything that
   contradicts it.

2. MAILING / PERSON state — where a person (PR, executor, decision maker,
   heir, owner) physically lives. Can be ANY US state. The decedent's
   executor might live in California; a probate heir might be in Texas.
   These come from extracting the printed address in the notice text.

   The trap is that LLM extractions hallucinate. Claude has been observed
   returning `owner_state="TN"` on Jefferson County AL probate notices
   whose text contained no "TN" / "Tennessee" anywhere. That's the
   2026-06-20 contamination we built this module to prevent.

   `validate_person_state()` is the guardrail: an extracted state is only
   accepted if the source text literally contains it (the 2-letter abbrev
   or the full name). Real out-of-state mailing addresses pass — the
   notice will say "Knoxville, TN 37920" or "Los Angeles, California
   90001" verbatim. Hallucinated states fail — there's no source-text
   anchor to point at. On rejection, we fall back to the property state
   as the safest guess.

This module has NO external dependencies — it's pure stdlib so it can be
imported from anywhere in the pipeline (parser, formatter, enricher)
without circular import risk.
"""

from __future__ import annotations

# Active production counties → state. Extend here when a new county is
# added; nothing else in the codebase should hardcode this mapping.
COUNTY_STATE: dict[str, str] = {
    # Alabama (active)
    "jefferson": "AL",
    "madison": "AL",
    "marshall": "AL",
    # Tennessee (legacy, kept for backward compat with the Knox/Blount era)
    "knox": "TN",
    "blount": "TN",
}

# Mapping of state abbrev → all the literal strings the source text might
# contain. Drives `validate_person_state()`. Covers every US state +
# DC + territories because mailing addresses can be anywhere.
_STATE_LITERALS: dict[str, tuple[str, ...]] = {
    "AL": ("AL", "Alabama", "Ala.", "Ala"),
    "AK": ("AK", "Alaska"),
    "AZ": ("AZ", "Arizona", "Ariz.", "Ariz"),
    "AR": ("AR", "Arkansas", "Ark.", "Ark"),
    "CA": ("CA", "California", "Calif.", "Calif", "Cal."),
    "CO": ("CO", "Colorado", "Colo.", "Colo"),
    "CT": ("CT", "Connecticut", "Conn.", "Conn"),
    "DE": ("DE", "Delaware", "Del."),
    "DC": ("DC", "District of Columbia", "Washington, DC", "Washington DC"),
    "FL": ("FL", "Florida", "Fla.", "Fla"),
    "GA": ("GA", "Georgia", "Ga."),
    "HI": ("HI", "Hawaii"),
    "ID": ("ID", "Idaho"),
    "IL": ("IL", "Illinois", "Ill.", "Ill"),
    "IN": ("IN", "Indiana", "Ind."),
    "IA": ("IA", "Iowa"),
    "KS": ("KS", "Kansas", "Kan.", "Kans."),
    "KY": ("KY", "Kentucky", "Ky."),
    "LA": ("LA", "Louisiana"),
    "ME": ("ME", "Maine"),
    "MD": ("MD", "Maryland", "Md."),
    "MA": ("MA", "Massachusetts", "Mass."),
    "MI": ("MI", "Michigan", "Mich."),
    "MN": ("MN", "Minnesota", "Minn."),
    "MS": ("MS", "Mississippi", "Miss."),
    "MO": ("MO", "Missouri", "Mo."),
    "MT": ("MT", "Montana", "Mont."),
    "NE": ("NE", "Nebraska", "Neb.", "Nebr."),
    "NV": ("NV", "Nevada", "Nev."),
    "NH": ("NH", "New Hampshire", "N.H."),
    "NJ": ("NJ", "New Jersey", "N.J."),
    "NM": ("NM", "New Mexico", "N.M.", "N. Mex."),
    "NY": ("NY", "New York", "N.Y."),
    "NC": ("NC", "North Carolina", "N.C.", "N. Car."),
    "ND": ("ND", "North Dakota", "N.D.", "N. Dak."),
    "OH": ("OH", "Ohio"),
    "OK": ("OK", "Oklahoma", "Okla.", "Okla"),
    "OR": ("OR", "Oregon", "Ore.", "Oreg."),
    "PA": ("PA", "Pennsylvania", "Pa.", "Penn."),
    "RI": ("RI", "Rhode Island", "R.I."),
    "SC": ("SC", "South Carolina", "S.C.", "S. Car."),
    "SD": ("SD", "South Dakota", "S.D.", "S. Dak."),
    "TN": ("TN", "Tennessee", "Tenn.", "Tenn"),
    "TX": ("TX", "Texas", "Tex."),
    "UT": ("UT", "Utah"),
    "VT": ("VT", "Vermont", "Vt."),
    "VA": ("VA", "Virginia", "Va."),
    "WA": ("WA", "Washington", "Wash."),
    "WV": ("WV", "West Virginia", "W.Va.", "W. Va."),
    "WI": ("WI", "Wisconsin", "Wisc.", "Wis."),
    "WY": ("WY", "Wyoming", "Wyo."),
    # Territories
    "PR": ("PR", "Puerto Rico", "P.R."),
    "VI": ("VI", "Virgin Islands", "U.S. Virgin Islands"),
    "GU": ("GU", "Guam"),
    "MP": ("MP", "Northern Mariana Islands"),
    "AS": ("AS", "American Samoa"),
}

# Default property state used when county is unknown. AL because every
# active county today is in AL; if Tennessee counties come back, this
# default is still safe (the county lookup wins). Returning "" instead
# would force every caller to handle the "no state" case, which is more
# disruptive than it's worth.
DEFAULT_PROPERTY_STATE = "AL"


def state_for_county(county: str | None) -> str:
    """The canonical PROPERTY state for a known county.

    Use this for `notice.state`, `notice.city`, `notice.zip` — anything
    that describes the property itself. Never use it for mailing /
    person fields (PR mailing address, decision maker state, heir state).

    Falls back to AL for unknown counties. The fallback is safe because
    every active pipeline is AL; if the lookup fails, the caller likely
    has a typo or new county that wasn't added to COUNTY_STATE.
    """
    return COUNTY_STATE.get((county or "").lower().strip(), DEFAULT_PROPERTY_STATE)


def validate_person_state(
    extracted_state: str | None,
    source_text: str,
    fallback_state: str = "",
) -> str:
    """Validate an extracted MAILING / PERSON state against source text.

    Use this whenever an LLM or regex extracts a state from a notice
    body — `owner_state` (PR mailing), `decision_maker_state`,
    `decedent_state`, heir states, etc. Returns the extracted state
    when it's anchored in the source text (real address); returns the
    fallback when the extracted value is missing, malformed, or absent
    from source (hallucinated).

    The rule: an extracted state is only kept when the raw notice text
    literally contains it — the 2-letter abbreviation OR the full name
    (case-insensitive, with common abbreviation variants like "Tenn.",
    "Calif.", "N.Y.").

    Why: legitimate out-of-state mailing addresses (a CA executor for
    an AL decedent, a TN heir of an AL probate) always appear in the
    source notice as printed text — "...Los Angeles, California 90001..."
    or "...Knoxville, TN 37920..." — so they pass cleanly. Hallucinated
    values (LLM returns "TN" but no "TN"/"Tennessee" appears anywhere)
    fail and the fallback wins.

    Args:
        extracted_state: What the LLM / regex returned. May be empty
            string, None, malformed (e.g. "Tennessee" instead of "TN",
            an integer, the full word), or a real 2-letter abbrev.
        source_text: The raw notice text the extraction came from.
            Usually `notice.raw_text`. Must contain the literal state
            for the extraction to be accepted.
        fallback_state: What to return when the extracted state is
            missing or fails validation. Pass the property state
            (`state_for_county(notice.county)`) for property-anchored
            fields; pass empty string when no sensible fallback exists.

    Returns:
        A 2-letter uppercase state code, or the fallback. Never returns
        a hallucinated value.
    """
    if not extracted_state:
        return fallback_state

    raw = (extracted_state or "").strip().upper()

    # Normalize full name → abbrev (LLMs return "Tennessee" sometimes)
    if len(raw) > 2:
        normalized = _normalize_state_name(raw)
        if normalized is None:
            return fallback_state
        raw = normalized

    if raw not in _STATE_LITERALS:
        return fallback_state

    # The source-text anchor check. Any literal form is enough.
    src_lower = source_text.lower()
    for literal in _STATE_LITERALS[raw]:
        # Bare 2-letter abbrev needs a word boundary so "AL" doesn't
        # match "ALABAMA" twice or "AL" inside "ALABAMA". For multi-char
        # literals, plain substring is fine.
        if len(literal) == 2:
            # Loose word-boundary check — preceded by space/comma/start,
            # followed by space/comma/digit/end. Handles the common
            # printed form "City, ST 12345".
            import re
            if re.search(rf"(?:^|[\s,.;:]){re.escape(literal)}(?=[\s,.;:0-9]|$)",
                         source_text, re.IGNORECASE):
                return raw
        else:
            if literal.lower() in src_lower:
                return raw

    # Extracted state not present in source → hallucination → fall back.
    return fallback_state


def _normalize_state_name(name: str) -> str | None:
    """Convert a state name to its 2-letter abbreviation. Returns None
    on unrecognized input."""
    upper = name.strip().upper()
    for abbrev, literals in _STATE_LITERALS.items():
        if upper == abbrev:
            return abbrev
        for lit in literals:
            if upper == lit.upper():
                return abbrev
    return None


def state_full_name(abbrev: str | None) -> str:
    """Return the full state name for a 2-letter abbreviation.

    Used by obituary / SSDI / Ancestry search code where the form
    field accepts the full state name ("Alabama" not "AL"). Falls
    back to the full name of DEFAULT_PROPERTY_STATE when input is
    empty or unrecognized.
    """
    if not abbrev:
        abbrev = DEFAULT_PROPERTY_STATE
    abbrev_upper = abbrev.strip().upper()
    # The first non-abbrev entry in _STATE_LITERALS is the canonical
    # full name (e.g. "Alabama"). Skip the 2-letter abbrev itself.
    literals = _STATE_LITERALS.get(abbrev_upper)
    if not literals:
        # Unknown abbrev — fall back to the default's full name.
        literals = _STATE_LITERALS[DEFAULT_PROPERTY_STATE]
    for lit in literals:
        if len(lit) > 2:
            return lit
    return abbrev_upper
