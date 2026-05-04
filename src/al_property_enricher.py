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
import re

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


def _enrich_madison(notice: NoticeData) -> bool:
    """Look up Madison County parcel + tax data and fill empty NoticeData fields.

    Returns True if any field was filled.
    """
    from madison_property_api import search_by_situs_address

    record = None
    for variant in _address_search_variants(notice.address):
        try:
            records = search_by_situs_address(variant)
        except Exception as e:
            logger.debug("Madison API error for %r: %s", variant, e)
            continue
        if records:
            record = records[0]
            logger.debug("Madison match on variant %r → parcel %s", variant, record.parcel_number)
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
