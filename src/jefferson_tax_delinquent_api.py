"""Jefferson County, AL bulk tax-delinquent / tax-sale list.

Pulls the official annual delinquent-parcel rosters for both county tax
divisions in one or two HTTP calls each. The data is published as inline
HTML tables on jccal.org under the Tax Collector's tax-lien-auction pages:

  Birmingham District: jccal.org/Default.asp?ID=2663 (landing page)
                       → /Sites/Jefferson_County/Documents/{year}/BirminghamTaxTable-{year}.html
  Bessemer District:   jccal.org/Default.asp?ID=2662 (landing page)
                       → /Sites/Jefferson_County/Documents/{year}/BessemerTaxTable-{year}.html

The landing pages are framed announcements; the actual table is iframed in
from the documents path. We fetch the table HTML directly. As of 2026, the
Birmingham table has ~12.8K parcels and Bessemer has ~5.4K (combined ~18K),
all part of the most recently published tax-lien auction roster.

Per the legal preamble on each page (AL Code § 40-10-180 et seq.), this list
IS the official tax-sale advertisement. The annual auction takes place at
[GovEase / eringcapture.jccal.org] in early May. Properties on this list
have already been advertised for the auction; many will redeem before the
sale, but the list itself is the canonical "going to tax sale" roster.

Mirrors the API surface of madison_tax_delinquent_api.py for cross-county
consistency. Phase 1 dollar-exposure-only filter strategy applies here too.

CLI:
    python src/jefferson_tax_delinquent_api.py
    python src/jefferson_tax_delinquent_api.py --district birmingham
    python src/jefferson_tax_delinquent_api.py --individuals-only --min-balance 5000
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from al_tax_year import current_al_tax_year

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)

BASE_URL = (
    "https://www.jccal.org/Sites/Jefferson_County/Documents/{year}/{district}TaxTable-{year}.html"
)

# Default high-exposure threshold (matches Madison adapter)
HIGH_EXPOSURE_THRESHOLD = 5000.0

# Splits "STREET CITY ST ZIP" into parts. Cities in Jefferson tax-table data
# are mostly the metro core, in ALL CAPS, with "BHAM" as the Birmingham
# abbreviation. Build a known-city list (longest-first) so we don't confuse
# directional suffixes ("SW", "NE") with city names.
_JEFFERSON_CITIES_UPPER: tuple[str, ...] = tuple(
    # Sorted longest-first so multi-word cities match before substrings.
    sorted(
        [
            "MOUNTAIN BROOK", "VESTAVIA HILLS", "PLEASANT GROVE", "CENTER POINT",
            "BIRMINGHAM", "BESSEMER", "HOOVER", "VESTAVIA", "HOMEWOOD",
            "TRUSSVILLE", "GARDENDALE", "IRONDALE", "LEEDS", "PINSON", "MORRIS",
            "CLAY", "WARRIOR", "FAIRFIELD", "MIDFIELD", "KIMBERLY", "GRAYSVILLE",
            "TARRANT", "BRIGHTON", "LIPSCOMB", "ADAMSVILLE", "TRAFFORD", "ARGO",
            "DORA", "FORESTDALE", "FULTONDALE", "BHAM",
        ],
        key=len,
        reverse=True,
    )
)
_SITUS_TAIL_RE = re.compile(r"\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class JeffersonDelinquentRecord:
    """One delinquent parcel from Jefferson's published tax-lien auction roster."""
    parcel_id: str               # ParcelNo: "22 00 31 3 012 003.000"
    lien_num: str                # LienNum (sequential within district)
    district: str                # "Birmingham" or "Bessemer"
    owner_name: str
    # Situs (property) address
    situs_address: str           # Parsed street portion of PropertyAddress
    situs_city: str
    situs_state: str
    situs_zip: str
    situs_raw: str               # The full unparsed PropertyAddress string
    # Mailing address (where tax bill goes)
    mailing_address: str
    mailing_city: str
    mailing_state: str
    mailing_zip: str
    # Valuation
    land_value: float
    building_value: float
    final_value: float           # Total appraised
    assessed_value: float        # AssdValue (typically a fraction of final_value)
    # Tax status
    tax_year: int
    balance_due: float           # AmtDue — total owed
    redemption_amount: float
    redemption_years: str
    legal_description: str       # Joined Legal1..Legal5
    # Derived
    is_individual_owner: bool
    is_high_exposure: bool

    @classmethod
    def from_row(cls, cells: list[str], district: str) -> "JeffersonDelinquentRecord":
        from config import BUSINESS_RE
        from jefferson_property_api import normalize_jefferson_city

        # Defensive — pad short rows with empty strings
        while len(cells) < 27:
            cells.append("")

        owner = cells[3].strip()
        balance = _parse_money(cells[22])
        situs_raw = cells[14].strip()
        situs_addr, situs_city, situs_state, situs_zip = _parse_situs(situs_raw)

        legal = " ".join(c.strip() for c in cells[15:20] if c.strip())

        is_individual = bool(owner) and not BUSINESS_RE.search(owner)
        is_high = balance >= HIGH_EXPOSURE_THRESHOLD

        return cls(
            parcel_id=cells[2].strip(),
            lien_num=cells[1].strip(),
            district=district,
            owner_name=owner,
            situs_address=situs_addr,
            situs_city=normalize_jefferson_city(situs_city),
            situs_state=situs_state,
            situs_zip=situs_zip,
            situs_raw=situs_raw,
            mailing_address=cells[4].strip(),
            mailing_city=normalize_jefferson_city(cells[7]),
            mailing_state=cells[8].strip(),
            mailing_zip=cells[9].strip(),
            land_value=_parse_money(cells[10]),
            building_value=_parse_money(cells[11]),
            final_value=_parse_money(cells[12]),
            assessed_value=_parse_money(cells[13]),
            tax_year=_parse_int(cells[0]),
            balance_due=balance,
            redemption_amount=_parse_money(cells[25]),
            redemption_years=cells[26].strip(),
            legal_description=legal,
            is_individual_owner=is_individual,
            is_high_exposure=is_high,
        )


# ── Parsing helpers ──────────────────────────────────────────────────


def _parse_money(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "").replace("$", "").strip())
    except ValueError:
        return 0.0


def _parse_int(s: str) -> int:
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _parse_situs(raw: str) -> tuple[str, str, str, str]:
    """Split 'STREET CITY ST ZIP' → (street, city, state, zip).

    Most Jefferson PropertyAddress strings are 'STREET ST ZIP' (no city).
    A subset includes a known abbreviated city like 'BHAM' or 'BESSEMER'.

    Strategy:
      1. Strip the trailing " <ST> <ZIP>" suffix — that part is unambiguous.
      2. Try the remaining tail against the known-Jefferson-cities list
         (longest-first). If matched, peel that off as the city.
      3. Otherwise leave city empty — never guess from the last token,
         because it's almost always a directional ("N"/"SW") or street
         suffix ("RD"/"DR"/"AVE"). Downstream Smarty enrichment fills
         the city from the ZIP.

    Returns (raw, "", "", "") only if the trailing state+zip can't be parsed.
    """
    if not raw:
        return ("", "", "", "")
    text = re.sub(r"\s+", " ", raw).strip()

    m = _SITUS_TAIL_RE.search(text)
    if not m:
        return (text, "", "", "")
    state, zip_code = m.group(1).strip(), m.group(2).strip()
    before = text[: m.start()].strip()
    before_upper = before.upper()

    # Try known-city match (longest first)
    for city in _JEFFERSON_CITIES_UPPER:
        suffix = " " + city
        if before_upper.endswith(suffix):
            address = before[: -len(suffix)].strip()
            return (address, city, state, zip_code)

    # No known city — leave empty rather than fabricating from a directional.
    return (before, "", state, zip_code)


# ── HTTP layer ───────────────────────────────────────────────────────


def _new_client(timeout: float = 120.0) -> httpx.Client:
    """Long timeout because the Birmingham table is ~8.4 MB."""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        verify=False,  # jccal.org cert chain occasionally has intermediate issues
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _fetch_district(client: httpx.Client, district: str, year: int) -> str:
    """GET the table HTML for one district. Returns the raw HTML."""
    url = BASE_URL.format(year=year, district=district)
    r = client.get(url)
    r.raise_for_status()
    # Server returns Windows-1252 — let httpx use the response's encoding
    if r.encoding is None or r.encoding.lower() not in ("windows-1252", "cp1252", "utf-8"):
        r.encoding = "cp1252"
    return r.text


def _parse_table(html: str, district: str) -> list[JeffersonDelinquentRecord]:
    """Extract delinquent records from the district HTML."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        logger.warning("No <table> found in %s district HTML", district)
        return []

    records: list[JeffersonDelinquentRecord] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds or len(tds) < 23:  # skip header / malformed rows
            continue
        cells = [td.get_text(strip=True) for td in tds]
        try:
            records.append(JeffersonDelinquentRecord.from_row(cells, district))
        except (ValueError, IndexError) as e:
            logger.debug("Skipping malformed row: %s", e)
    return records


# ── Public API ───────────────────────────────────────────────────────


def fetch_delinquent_parcels(
    *,
    district: str = "both",          # "birmingham" | "bessemer" | "both"
    year: int | None = None,
    individuals_only: bool = False,
    min_balance: float = 0.0,
) -> list[JeffersonDelinquentRecord]:
    """Pull Jefferson County's published delinquent-parcel auction roster.

    Phase 1 dollar-exposure focus matches the Madison adapter — the published
    Jefferson list IS the next May auction's advertisement (per AL § 40-10-180),
    so timeline-based filtering doesn't add value over a balance threshold.

    Args:
        district: ``"birmingham"`` (~12.8K parcels), ``"bessemer"`` (~5.4K),
            or ``"both"`` (default). Birmingham covers most of Jefferson;
            Bessemer is the southwestern division (Bessemer + surrounding cities).
        year: Tax year to fetch. Defaults to the most recently published roster.
        individuals_only: When True, drop entity-owned parcels (LLC, Inc, etc.
            via ``config.BUSINESS_RE``). Trusts and "(HEIRS OF)" pass through.
        min_balance: Drop records with ``balance_due`` below this threshold.
            Recommended: ``min_balance=5000`` for high-exposure focus.

    Returns:
        List of JeffersonDelinquentRecord. Empty on network/parse failure.

    Raises:
        ValueError on invalid district.
        httpx.HTTPError on transport failure.
    """
    district = (district or "both").lower()
    if district not in ("birmingham", "bessemer", "both"):
        raise ValueError(f"district must be 'birmingham', 'bessemer', or 'both'; got {district!r}")
    if year is None:
        year = current_al_tax_year()

    targets = ["Birmingham", "Bessemer"] if district == "both" else [district.title()]
    all_records: list[JeffersonDelinquentRecord] = []

    with _new_client() as client:
        for d in targets:
            logger.info("Fetching Jefferson %s district (year %d)…", d, year)
            html = _fetch_district(client, d, year)
            recs = _parse_table(html, d)
            logger.info("  %s: %d raw records", d, len(recs))
            all_records.extend(recs)

    if individuals_only:
        all_records = [r for r in all_records if r.is_individual_owner]
    if min_balance > 0:
        all_records = [r for r in all_records if r.balance_due >= min_balance]

    return all_records


def to_notice_data(rec: JeffersonDelinquentRecord) -> "NoticeData":
    """Convert a delinquent record into a NoticeData for the enrichment pipeline.

    Sets notice_type="tax_sale" because Jefferson's published list IS the
    annual tax-lien auction advertisement — every parcel here is scheduled
    for sale unless it redeems. Caller can downgrade to "tax_delinquent" if
    they want to distinguish from the Madison `is_tax_sale_parcel`-only subset.
    """
    from notice_parser import NoticeData

    today = datetime.now().strftime("%Y-%m-%d")
    return NoticeData(
        county="Jefferson",
        state="AL",
        notice_type="tax_sale",
        date_added=today,
        received_date=today,
        # Property identity
        owner_name=rec.owner_name,
        tax_owner_name=rec.owner_name,
        address=rec.situs_address,
        city=rec.situs_city,
        zip=rec.situs_zip,
        parcel_id=rec.parcel_id,
        # Mailing address (PR-style slot for the owner mailing)
        owner_street=rec.mailing_address,
        owner_city=rec.mailing_city,
        owner_state=rec.mailing_state or "AL",
        owner_zip=rec.mailing_zip,
        # Tax delinquency fields
        tax_delinquent_amount=f"{rec.balance_due:.2f}",
        tax_delinquent_years="",  # Jefferson publishes one year per roster; not multi-year per record
        # Property valuation (assessor's last-assessed)
        assessed_value=f"{rec.final_value:.0f}" if rec.final_value > 0 else "",
        # Jefferson DispCode-equivalent: include the district tag in municipality
        municipality=rec.district,
        # Synthesized source URL — points back to the district's announcement page
        source_url=(
            f"https://www.jccal.org/Default.asp?ID="
            f"{'2663' if rec.district == 'Birmingham' else '2662'}"
        ),
        raw_text=(
            f"TAX SALE — {rec.district} District — Parcel {rec.parcel_id} "
            f"(tax year {rec.tax_year}, ${rec.balance_due:,.2f} owed) — "
            f"{rec.legal_description}"
        ),
    )


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    district = "both"
    year = current_al_tax_year()
    individuals_only = "--individuals-only" in argv
    min_balance = 0.0
    for i, a in enumerate(argv):
        if a == "--district" and i + 1 < len(argv):
            district = argv[i + 1].lower()
        elif a == "--year" and i + 1 < len(argv):
            try:
                year = int(argv[i + 1])
            except ValueError:
                pass
        elif a == "--min-balance" and i + 1 < len(argv):
            try:
                min_balance = float(argv[i + 1])
            except ValueError:
                pass

    records = fetch_delinquent_parcels(
        district=district,
        year=year,
        individuals_only=individuals_only,
        min_balance=min_balance,
    )

    print(f"\nJefferson delinquent parcels: {len(records)}  (district={district}, year={year})")
    if individuals_only:
        print("  (filter: individuals only — entities dropped)")
    if min_balance > 0:
        print(f"  (filter: balance_due >= ${min_balance:,.2f})")

    if not records:
        return 0

    total_balance = sum(r.balance_due for r in records)
    total_appraised = sum(r.final_value for r in records)
    by_district: dict[str, int] = {}
    for r in records:
        by_district[r.district] = by_district.get(r.district, 0) + 1
    print(f"  Total balance owed:    ${total_balance:,.2f}")
    print(f"  Total appraised value: ${total_appraised:,.2f}")
    print(f"  By district: {by_district}")

    print("\nTop 10 by balance owed:")
    top = sorted(records, key=lambda r: r.balance_due, reverse=True)[:10]
    for r in top:
        print(
            f"  {r.parcel_id}  {r.owner_name[:38]:38s}  "
            f"{r.situs_address[:28]:28s}  {r.situs_city:12s}  ${r.balance_due:>10,.2f}"
        )

    print("\nFirst record (full):")
    print(json.dumps(asdict(records[0]), indent=2))

    notice = to_notice_data(records[0])
    print("\nFirst record as NoticeData (key fields):")
    for f in ("county", "notice_type", "owner_name", "address", "city", "zip", "parcel_id",
              "tax_delinquent_amount", "assessed_value", "municipality"):
        print(f"  {f:24s} = {getattr(notice, f)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
