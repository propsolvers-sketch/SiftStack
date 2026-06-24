"""Jefferson County, AL property search via the E-Ring Capture API.

The eringcapture.jccal.org SPA is JavaScript-rendered (Imperva/Incapsula bot
protection on the front door), but its underlying REST endpoint is a clean
JSON POST that can be called directly without browser automation:

    POST https://jeffersonexpress.capturecama.com/SearchRP
    Content-Type: application/json
    Body: { tenantUrl, expressUrl, reserved, searchstring, searchtype, recordyear }

Used by the probate enrichment pipeline to map a decedent (or PR) name to one
or more parcels in Jefferson County (Birmingham + Bessemer divisions).

CLI usage:
    python src/jefferson_property_api.py SMITH
    python src/jefferson_property_api.py "FULENWIDER ORVELENE"
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterator

import httpx

from al_tax_year import current_al_tax_year

logger = logging.getLogger(__name__)

API_URL = "https://jeffersonexpress.capturecama.com/SearchRP"
TENANT_URL = "https://eringcapture.jccal.org"
EXPRESS_URL = "https://jeffersonexpress.capturecama.com"

# searchtype codes observed in live traffic
# Confirmed via live SPA recon — values match the option list in the
# search-type dropdown at https://eringcapture.jccal.org/propsearch
_SEARCH_TYPE_OWNER = "1"
_SEARCH_TYPE_PARCEL = "2"
_SEARCH_TYPE_MAILING_ADDRESS = "3"
_SEARCH_TYPE_PROPERTY_ADDRESS = "4"

# Directional tokens that the assessor's index sometimes omits between the
# house number and street name (e.g. "1124 SW 16TH ST" stored as "1124 16TH ST").
_LEADING_DIRECTIONAL_RE = re.compile(
    r"^(\d+)\s+(?:NE|NW|SE|SW|N|S|E|W)\s+",
    re.IGNORECASE,
)

# Jefferson assessor's DispCode is mostly the city name in ALL CAPS, but a few
# codes are abbreviations that we normalize to readable display names.
# Everything not in this map is title-cased; "COUNTY" (unincorporated) is left
# uppercase as a clear flag for downstream filters.
_DISP_CODE_DISPLAY = {
    "BHAM": "Birmingham",
    "B'HAM": "Birmingham",      # apostrophe variant observed in DispCode
    "B'HAN": "Birmingham",      # likely typo of B'HAM — Ronald L. Brown record 2026-06-24
    "BHM": "Birmingham",        # short-form variant
}


def _normalize_municipality(disp_code: str) -> str:
    code = (disp_code or "").strip()
    if not code:
        return ""
    upper = code.upper()
    if upper in _DISP_CODE_DISPLAY:
        return _DISP_CODE_DISPLAY[upper]
    if upper == "COUNTY":
        return "COUNTY"  # flag for unincorporated Jefferson; keep distinguishable
    return code.title()


# ── Postal city normalization (shared across Jefferson adapters) ──────
# The Jefferson assessor publishes postal cities in their abbreviated /
# raw form ("BHAM", "VESTAVIA"). For DataSift display + Smarty matching,
# expand the well-known abbreviations and title-case the rest. Any module
# that surfaces a Jefferson postal city should pipe it through this.
_CITY_DISPLAY_MAP = {
    "BHAM": "Birmingham",
    "B'HAM": "Birmingham",               # apostrophe variant from Jefferson's PostalCity
    "B'HAN": "Birmingham",               # likely typo of B'HAM — observed 2026-06-24
    "BHM": "Birmingham",                 # short-form variant
    "VESTAVIA": "Vestavia Hills",        # actual incorporated city name
    "MT OLIVE": "Mount Olive",
    "MOUNTAIN BRK": "Mountain Brook",    # abbreviation seen in mailing addresses
    "MTN BROOK": "Mountain Brook",
    "PLEASANT GR": "Pleasant Grove",
}


def normalize_jefferson_city(city: str) -> str:
    """Expand 'BHAM' → 'Birmingham' (etc.) and title-case all other cities."""
    raw = (city or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in _CITY_DISPLAY_MAP:
        return _CITY_DISPLAY_MAP[upper]
    return raw.title()


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class JeffersonPropertyRecord:
    """One row from the Jefferson County real-property search."""
    parcel_number: str       # "05 00 14 0 000 045.000"
    account_number: str      # Tax account number (matches AccountNo / KeyNumber)
    owner_name: str          # "SMITH OPAL W" or "SMITH ADAM & SMITH FLYNN"
    # Situs (property) address — the actual physical location
    situs_address: str
    situs_city: str
    situs_state: str
    situs_zip: str
    # Mailing address (where tax bills go) — sometimes differs from situs
    mailing_address: str
    mailing_city: str
    mailing_state: str
    mailing_zip: str
    # Valuation
    land_value: float        # Land value
    improvement_value: float # ImpValue
    total_value: float       # Total appraised
    assessed_value: float    # Assessed (typically a fraction of total)
    taxable_value: float
    # Tax status
    total_tax: float
    total_paid: float
    fee_due: float
    is_delinquent: bool      # tax_lien_count > 0 OR total_tax > total_paid + fee_due
    tax_lien_count: int
    tax_sale_flag: str       # Raw TaxSale field
    exemption_code: str      # Homestead / over-65 / disabled / etc.
    is_homestead: bool       # Likely owner-occupied primary residence
    property_use: str        # Friendly assessor classification ("Residential", "Commercial", "Other")
    municipality: str        # Jefferson assessor's DispCode: BHAM, TRUSSVILLE, HOOVER, COUNTY (unincorporated), etc.

    @classmethod
    def from_api_record(cls, raw: dict) -> "JeffersonPropertyRecord":
        total_tax = float(raw.get("TotalTax") or 0)
        total_paid = float(raw.get("TotalPaid") or 0)
        fee_due = float(raw.get("FeeDue") or 0)
        lien_count = int(raw.get("TaxLienCount") or 0)
        delinquent = lien_count > 0 or (total_tax > 0 and total_paid + 0.01 < total_tax)

        situs = (raw.get("PropAddr1") or "").strip()
        mailing = (raw.get("Address1") or "").strip()
        improvement = float(raw.get("ImpValue") or 0)
        exempt = (raw.get("ExmtCode") or "").strip()

        # AL property classification (AssmtClass code → friendly name).
        # Class I = utility, II = commercial, III = residential, IV = motor vehicle.
        _class_map = {"1": "Utility", "2": "Commercial", "3": "Residential", "4": "Vehicle"}
        prop_use = _class_map.get((raw.get("AssmtClass") or "").strip(), "Other")
        # Homestead heuristic: has a structure, mailing matches situs (owner-occupied),
        # and either a non-empty exemption code or non-zero improvement value. Excludes
        # vacant lots and off-site investor-owned rentals.
        homestead = bool(
            situs and improvement > 0
            and mailing and situs.upper() == mailing.upper()
            and (exempt or improvement > 10000)
        )

        return cls(
            parcel_number=(raw.get("ParcelNo") or "").strip(),
            account_number=(raw.get("AccountNo") or "").strip(),
            owner_name=" ".join((raw.get("MigratedOwners") or "").split()),  # collapse double-spaces
            situs_address=situs,
            situs_city=normalize_jefferson_city(raw.get("PropCity") or ""),
            situs_state=(raw.get("PropState") or "").strip(),
            situs_zip=(raw.get("PropZip") or "").strip(),
            mailing_address=mailing,
            mailing_city=normalize_jefferson_city(raw.get("City") or ""),
            mailing_state=(raw.get("State") or "").strip(),
            mailing_zip=(raw.get("Zip") or "").strip(),
            land_value=float(raw.get("LandValue") or 0),
            improvement_value=improvement,
            total_value=float(raw.get("TotalValue") or 0),
            assessed_value=float(raw.get("AssessedValue") or 0),
            taxable_value=float(raw.get("TaxableValue") or 0),
            total_tax=total_tax,
            total_paid=total_paid,
            fee_due=fee_due,
            is_delinquent=delinquent,
            tax_lien_count=lien_count,
            tax_sale_flag=(raw.get("TaxSale") or "").strip(),
            exemption_code=exempt,
            is_homestead=homestead,
            property_use=prop_use,
            municipality=_normalize_municipality(raw.get("DispCode") or ""),
        )


# ── HTTP layer ───────────────────────────────────────────────────────


def _new_client(timeout: float = 30.0) -> httpx.Client:
    # NOTE: jeffersonexpress.capturecama.com serves an incomplete SSL chain —
    # browsers/curl-system trust resolve the intermediate via macOS/Windows
    # keychain, but Python's certifi bundle does not. The host is fixed and
    # owned by Jefferson County's vendor, so verify=False is acceptable here;
    # the alternative would be shipping the intermediate bundle.
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        verify=False,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": TENANT_URL,
            "Referer": f"{TENANT_URL}/",
            "referring-page": f"{TENANT_URL}/propsearch",
        },
    )


def _post_search(
    client: httpx.Client,
    search_string: str,
    search_type: str,
    year: int,
) -> list[dict]:
    payload = {
        "tenantUrl": TENANT_URL,
        "expressUrl": EXPRESS_URL,
        "reserved": "",
        "searchstring": search_string,
        "searchtype": search_type,
        "recordyear": str(year),
    }
    r = client.post(API_URL, content=json.dumps(payload))
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        logger.warning("Unexpected response shape: %s", type(data))
        return []
    return data


def _iter_records(rows: list[dict]) -> Iterator[JeffersonPropertyRecord]:
    for raw in rows:
        try:
            yield JeffersonPropertyRecord.from_api_record(raw)
        except (TypeError, ValueError) as e:
            logger.debug("skip malformed record: %s", e)


# ── Public API ───────────────────────────────────────────────────────


def search_by_owner_name(
    name: str,
    *,
    year: int | None = None,
) -> list[JeffersonPropertyRecord]:
    """Search Jefferson County properties by owner name.

    Args:
        name: Owner search string (last name first, optionally with first name).
            Examples: "SMITH", "SMITH OPAL", "FULENWIDER ORVELENE".
            The county search is prefix-matching from the start of the recorded
            owner string, so include just the surname for broadest coverage.
        year: Tax year. Defaults to ``current_al_tax_year()`` — last year's
            roll before the May auction, current year after. 2016 is the
            earliest available.

    Returns:
        List of JeffersonPropertyRecord. Empty if no matches.

    Raises:
        httpx.HTTPError on network/server failure.
        ValueError if ``name`` is empty.
    """
    if not name or not name.strip():
        raise ValueError("name is required")
    if year is None:
        year = current_al_tax_year()

    # Jefferson's MigratedOwners field is INCONSISTENT: some parcels use a
    # double space between surname and given name(s) (e.g. "SMITH  OPAL W")
    # while others use single space (e.g. "LANEY HARVEY JACK & ..."). The API
    # prefix-matches literally, so we have to try both. Try double-space first
    # (preserves coverage for SMITH-style records) and fall back to single-space
    # when nothing matches.
    cleaned = " ".join(name.strip().upper().split())
    parts = cleaned.split(" ", 1)

    with _new_client() as client:
        if len(parts) == 1:
            rows = _post_search(client, cleaned, _SEARCH_TYPE_OWNER, year)
        else:
            double = f"{parts[0]}  {parts[1]}"
            rows = _post_search(client, double, _SEARCH_TYPE_OWNER, year)
            if not rows:
                rows = _post_search(client, cleaned, _SEARCH_TYPE_OWNER, year)

    return list(_iter_records(rows))


def search_by_situs_address(
    address: str,
    *,
    year: int | None = None,
) -> list[JeffersonPropertyRecord]:
    """Search Jefferson County properties by situs (property) address.

    The E-Ring API uses prefix-matching against the recorded ``PropAddr1``
    field — the more characters of the address you pass, the narrower the
    match. Caller usually passes the street-and-number string; if zero hits
    come back, the helper retries with leading directionals stripped
    (e.g. ``"1124 SW 16TH ST SW"`` → ``"1124 16TH ST SW"``) since the
    assessor occasionally indexes addresses without the leading directional.

    Args:
        address: Full or partial property address. Examples:
            ``"305 OREGON ST"``, ``"1825 MONTCLAIR RD"``,
            ``"3042 BOSWELL DR NW"``.
        year: Tax year. Defaults to ``current_al_tax_year()``.

    Returns:
        List of JeffersonPropertyRecord. Empty list if no matches survive
        either the original or directional-stripped query.

    Raises:
        ValueError if ``address`` is empty.
        httpx.HTTPError on network/server failure.
    """
    if not address or not address.strip():
        raise ValueError("address is required")
    if year is None:
        year = current_al_tax_year()

    primary = " ".join(address.strip().upper().split())

    with _new_client() as client:
        rows = _post_search(client, primary, _SEARCH_TYPE_PROPERTY_ADDRESS, year)
        if not rows:
            # Retry without the leading directional — the assessor occasionally
            # stores "1124 16TH ST" instead of "1124 SW 16TH ST".
            stripped = _LEADING_DIRECTIONAL_RE.sub(r"\1 ", primary)
            if stripped != primary:
                rows = _post_search(client, stripped, _SEARCH_TYPE_PROPERTY_ADDRESS, year)

    return list(_iter_records(rows))


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if not argv:
        print("Usage: python jefferson_property_api.py NAME [YEAR]", file=sys.stderr)
        return 2

    name = argv[0]
    year = int(argv[1]) if len(argv) > 1 else current_al_tax_year()

    print(f"Searching Jefferson County for owner: {name!r} (year {year})")
    records = search_by_owner_name(name, year=year)
    print(f"Found {len(records)} parcels.\n")

    for r in records[:20]:
        flag = " [DELINQUENT]" if r.is_delinquent else ""
        situs = r.situs_address or "(no situs)"
        print(
            f"  {r.parcel_number}  {r.owner_name:42s}  "
            f"{situs:35s}  {r.situs_city:15s}{flag}"
        )

    if len(records) > 20:
        print(f"\n  ... {len(records) - 20} more records suppressed")

    if records:
        print("\nFirst record (full):")
        print(json.dumps(asdict(records[0]), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
