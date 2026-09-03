"""Shared AL street-suffix abbreviation map + canonical street normalizer.

Alabama county assessors (Jefferson eringcapture, Madison/Marshall
AssuranceWeb) and DataSift BOTH store the ABBREVIATED suffix form —
``DR`` not ``Drive``, ``CV`` not ``Cove``, ``RD`` not ``Road``. Our source
portals are inconsistent: the T&B trustee portal publishes
``807 Briarwood Drive`` while DataSift holds ``807 Briarwood Dr``.

This module is the SINGLE SOURCE OF TRUTH for that mapping. It is
deliberately zero-dependency — no project imports, no third-party imports —
so ``datasift_api`` (stdlib + requests only) can use it without dragging in
``notice_parser`` or the smartystreets SDK.

Do not fork this table. If a suffix is missing, add it HERE.
"""
from __future__ import annotations


# Common AL street-suffix abbreviations (full → assessor-format)
SUFFIX_ABBR = {
    "COVE": "CV", "DRIVE": "DR", "ROAD": "RD", "STREET": "ST",
    "AVENUE": "AVE", "COURT": "CT", "CIRCLE": "CIR", "LANE": "LN",
    "PLACE": "PL", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "TERRACE": "TER", "TRAIL": "TRL", "TRACE": "TRCE", "POINT": "PT",
    "RIDGE": "RDG", "CROSSING": "XING", "LANDING": "LNDG", "HOLLOW": "HOLW",
}


def canonical_street(street: str) -> str:
    """Collapse a street string to the canonical comparison form.

    Normalization is intentionally conservative: only the trailing street
    suffix token (or the second-to-last token when a trailing directional
    follows it, e.g. ``DRIVE SW``) is rewritten. The house number and the
    street name itself are never altered.

    Because both spellings collapse to the abbreviated form, an already
    abbreviated input passes through unchanged apart from case and
    whitespace — which is what makes BOTH directions compare equal::

        canonical_street("807 Briarwood Drive") == "807 briarwood dr"
        canonical_street("807 Briarwood Dr")    == "807 briarwood dr"

    Returns "" for empty / whitespace-only input.
    """
    if not street or not street.strip():
        return ""

    cleaned = street.upper().replace(".", "").replace(",", "")
    parts = cleaned.split()
    if not parts:
        return ""

    if parts[-1] in SUFFIX_ABBR:
        parts[-1] = SUFFIX_ABBR[parts[-1]]
    elif len(parts) >= 3 and parts[-2] in SUFFIX_ABBR:
        # Suffix sits second-to-last, trailing directional (e.g. "DRIVE SW")
        parts[-2] = SUFFIX_ABBR[parts[-2]]

    return " ".join(parts).lower()
