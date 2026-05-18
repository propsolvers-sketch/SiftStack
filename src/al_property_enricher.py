"""Enrich Alabama notices with county assessor data (Jefferson + Madison).

Fills the AL-county property fields that no other enrichment step covers:
  parcel_id, assessed_value, property_use, municipality, is_homestead,
  tax_delinquent_amount, tax_delinquent_years

Source data:
  Jefferson → eringcapture.jccal.org via jefferson_property_api.py
  Madison   → AssuranceWeb via madison_property_api.py

Both county APIs use prefix-matching against an indexed situs string. Two
real-world quirks force address normalization before search:
  1. Suffixes are stored abbreviated (CV not Cove, DR not Drive, RD not Road).
  2. Prefix-matching means a too-long query returns 0 hits where a shorter
     stem would have matched — so we fall back to suffix-stripped variants.
"""
from __future__ import annotations

import logging

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Common AL street-suffix abbreviations (full → assessor-format)
_SUFFIX_ABBR = {
    "COVE": "CV", "DRIVE": "DR", "ROAD": "RD", "STREET": "ST",
    "AVENUE": "AVE", "COURT": "CT", "CIRCLE": "CIR", "LANE": "LN",
    "PLACE": "PL", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "TERRACE": "TER", "TRAIL": "TRL", "TRACE": "TRCE", "POINT": "PT",
    "RIDGE": "RDG", "CROSSING": "XING", "LANDING": "LNDG", "HOLLOW": "HOLW",
}


def _address_search_variants(address: str) -> list[str]:
    """Return search variants in order of specificity.

    Tries: original → suffix-abbreviated → suffix-stripped (prefix match).
    """
    if not address.strip():
        return []
    upper = " ".join(address.strip().upper().split())
    variants = [upper]

    # Variant 2: abbreviate trailing suffix word
    parts = upper.split()
    if len(parts) >= 2:
        last = parts[-1].rstrip(".")
        if last in _SUFFIX_ABBR:
            variants.append(" ".join(parts[:-1] + [_SUFFIX_ABBR[last]]))
        elif len(parts) >= 3:
            # Suffix may be second-to-last (e.g. "DR SW")
            second_last = parts[-2].rstrip(".")
            if second_last in _SUFFIX_ABBR:
                rebuilt = parts[:-2] + [_SUFFIX_ABBR[second_last], parts[-1]]
                variants.append(" ".join(rebuilt))

    # Variant 3: strip trailing suffix entirely (relies on prefix matching)
    if len(parts) >= 3:
        variants.append(" ".join(parts[:-1]))

    # Dedupe while preserving order
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _enrich_jefferson(notice: NoticeData) -> bool:
    """Look up Jefferson County parcel + tax data and fill empty NoticeData fields.

    Returns True if any field was filled.
    """
    from jefferson_property_api import search_by_situs_address

    record = None
    for variant in _address_search_variants(notice.address):
        try:
            records = search_by_situs_address(variant)
        except Exception as e:
            logger.debug("Jefferson API error for %r: %s", variant, e)
            continue
        if records:
            record = records[0]
            logger.debug("Jefferson match on variant %r → parcel %s", variant, record.parcel_number)
            break

    if not record:
        logger.debug("Jefferson: no parcel match for %r", notice.address)
        return False

    filled = False
    if not notice.parcel_id and record.parcel_number:
        notice.parcel_id = record.parcel_number
        filled = True
    if not notice.assessed_value and record.assessed_value:
        notice.assessed_value = f"{record.assessed_value:.0f}"
        filled = True
    if not notice.property_use and record.property_use:
        notice.property_use = record.property_use
        filled = True
    if not notice.municipality and record.municipality:
        notice.municipality = record.municipality
        filled = True
    if not notice.is_homestead and record.is_homestead:
        notice.is_homestead = "Y"
        filled = True
    if not notice.tax_delinquent_amount and record.is_delinquent:
        unpaid = (record.total_tax or 0) - (record.total_paid or 0) + (record.fee_due or 0)
        if unpaid > 0:
            notice.tax_delinquent_amount = f"{unpaid:.2f}"
            notice.tax_delinquent_years = str(record.tax_lien_count) if record.tax_lien_count else "1"
            filled = True
    return filled


def _split_address_for_madison(address: str) -> tuple[str, str] | None:
    """Split a street address into (number, root_street_name) for Madison's
    AssuranceWeb search, which takes them as separate criteria fields and
    indexes street names without suffix or directional tokens.

    Examples:
        "3037 Box Canyon Rd Se"      → ("3037", "BOX CANYON")
        "118 Trenton Creek Lane"     → ("118", "TRENTON CREEK")
        "120 Oakland Church"         → ("120", "OAKLAND CHURCH")
        "Lot 6, The Crossings ..."   → None (legal description, not address)
    """
    parts = address.strip().upper().split()
    if not parts or not parts[0].isdigit():
        return None
    number = parts[0]
    rest = parts[1:]

    # Strip trailing direction tokens (NE, NW, SE, SW, N, S, E, W and full words)
    _DIRS = {
        "NE", "NW", "SE", "SW", "N", "S", "E", "W",
        "NORTHEAST", "NORTHWEST", "SOUTHEAST", "SOUTHWEST",
        "NORTH", "SOUTH", "EAST", "WEST",
    }
    while rest and rest[-1].rstrip(".") in _DIRS:
        rest.pop()

    # Strip trailing street-suffix word (full or abbreviation)
    _SUFFIX_WORDS = (
        set(_SUFFIX_ABBR.keys())
        | set(_SUFFIX_ABBR.values())
        | {"WAY", "RUN", "PASS", "PATH", "LOOP", "WALK", "ROW", "GLEN", "GLENN", "BEND"}
    )
    while rest and rest[-1].rstrip(".") in _SUFFIX_WORDS:
        rest.pop()

    if not rest:
        return None
    return (number, " ".join(rest))


def _enrich_madison(notice: NoticeData) -> bool:
    """Look up Madison County parcel + tax data and fill empty NoticeData fields.

    Returns True if any field was filled.
    """
    from madison_property_api import search_by_situs_address

    parsed = _split_address_for_madison(notice.address)
    if not parsed:
        logger.debug("Madison: address %r doesn't parse to (number, name)", notice.address)
        return False
    number, name = parsed

    record = None
    # Try full multi-word name first, then progressively shorter prefixes —
    # Madison's index sometimes has only the first word of compound names.
    name_variants = [name]
    name_parts = name.split()
    if len(name_parts) > 1:
        name_variants.append(name_parts[0])  # fall back to first word only

    for variant in name_variants:
        try:
            records = search_by_situs_address(number, variant)
        except Exception as e:
            logger.debug("Madison API error for (%r, %r): %s", number, variant, e)
            continue
        if records:
            record = records[0]
            logger.debug(
                "Madison match on (%r, %r) → parcel %s",
                number, variant, record.parcel_number,
            )
            break

    if not record:
        logger.debug("Madison: no parcel match for %r", notice.address)
        return False

    filled = False
    if not notice.parcel_id and record.parcel_number:
        notice.parcel_id = record.parcel_number
        filled = True
    if not notice.property_use and record.property_use:
        notice.property_use = record.property_use
        filled = True
    if not notice.tax_delinquent_amount and record.is_delinquent and record.balance_due > 0:
        notice.tax_delinquent_amount = f"{record.balance_due:.2f}"
        notice.tax_delinquent_years = "1"  # Madison's search response doesn't expose lien count
        filled = True
    return filled


def enrich_al_properties(notices: list[NoticeData]) -> int:
    """Fill parcel + assessor + tax-delinquency fields for AL notices.

    Returns count of notices where at least one field was filled.
    """
    count = 0
    for n in notices:
        if not n.address.strip():
            continue
        county_lower = n.county.lower().strip()
        try:
            if county_lower == "jefferson":
                if _enrich_jefferson(n):
                    count += 1
            elif county_lower == "madison":
                if _enrich_madison(n):
                    count += 1
        except ImportError as e:
            logger.warning("AL property API module unavailable: %s", e)
            return count
        except Exception as e:
            logger.warning(
                "AL property enrichment failed for %s (%s): %s",
                n.address, n.county, e,
            )
    return count
