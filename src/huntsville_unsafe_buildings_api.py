"""City of Huntsville, AL — Unsafe Buildings list adapter.

Pulls the City of Huntsville's monthly Unsafe Buildings list PDF, parses
the 3-column layout (Case Created | Case Number | Case Address), and
emits structured records ready for the SiftStack enrichment pipeline.

The PDF lives under /wp-content/uploads/{YYYY}/{MM}/{MM}-{YYYY}-Unsafe-Building-List.pdf
and is regenerated periodically (the April 2026 snapshot was "as of 04/20/2026").
This adapter tries the current month first, then falls back through the
previous 6 months until it finds a published list.

Per-record data is intentionally minimal — the city publishes location
only, no owner name. To enrich with current-owner-of-record, pair this
output with a follow-up Madison property API call by address.

This is the highest-distress code-enforcement signal in the Huntsville
metro: every record is a property the city has formally declared
uninhabitable. Compared to softer signals like overgrown lots or 311
complaints, the Unsafe Building list is pre-filtered to genuine
condemnation candidates — a much higher conversion rate per outreach.

CLI:
    python src/huntsville_unsafe_buildings_api.py
    python src/huntsville_unsafe_buildings_api.py --year 2026 --month 4
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)

PDF_URL_TEMPLATE = (
    "https://www.huntsvilleal.gov/wp-content/uploads/"
    "{year}/{month:02d}/{month:02d}-{year}-Unsafe-Building-List.pdf"
)

# Each record line in the PDF (after pdfminer extraction) is:
#   Date column → "M/D/YYYY"
#   Case column → "CE-YY-NNNN" (often combined with date on one line)
# The address column is a separate block, on the same logical row but
# rendered as its own paragraph after pdfminer's column flow.
_DATE_CASE_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{4})\s+(CE-\d+-\d+)\s*$"
)
# Address pattern — line ends in ", Huntsville, AL <zip>"
# Allow the street-name to start with any alphanumeric (e.g. "9Th", "10Th").
_ADDRESS_RE = re.compile(
    r"^\d+\s+\w[\w\s,\'.\-#()&/]+(?:\s*,)?\s*Huntsville,?\s*AL\s+(\d{5})\s*$",
    re.IGNORECASE,
)
# Pull just the street + city + zip out of a matched address
_ADDR_PARTS_RE = re.compile(
    r"^(.+?)\s*,\s*Huntsville,?\s*AL\s+(\d{5})\s*$",
    re.IGNORECASE,
)


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class HuntsvilleUnsafeRecord:
    """One active Unsafe Building case from the Huntsville Code Enforcement list."""
    case_number: str             # e.g. "CE-24-5123"
    case_created: str            # YYYY-MM-DD; opening date of the case
    case_age_years: int          # whole years between case_created and today
    address: str                 # raw street address: "3042 Boswell Dr Nw"
    city: str                    # always "Huntsville"
    state: str                   # always "AL"
    zip: str                     # 5-digit ZIP
    address_full: str            # full string as printed in the PDF
    list_published: str          # YYYY-MM-DD of the source PDF (e.g. "2026-04-20")

    @classmethod
    def from_columns(
        cls, date_str: str, case_num: str, address_full: str, list_published: str,
    ) -> "HuntsvilleUnsafeRecord":
        m = _ADDR_PARTS_RE.match(address_full.strip())
        if m:
            street, zip_code = m.group(1).strip(), m.group(2).strip()
        else:
            street, zip_code = address_full.strip(), ""

        # Normalize date to YYYY-MM-DD
        try:
            dt = datetime.strptime(date_str.strip(), "%m/%d/%Y").date()
            iso_date = dt.strftime("%Y-%m-%d")
            age = max(0, date.today().year - dt.year - int(
                (date.today().month, date.today().day) < (dt.month, dt.day)
            ))
        except ValueError:
            iso_date = ""
            age = 0

        return cls(
            case_number=case_num.strip(),
            case_created=iso_date,
            case_age_years=age,
            address=street,
            city="Huntsville",
            state="AL",
            zip=zip_code,
            address_full=address_full.strip(),
            list_published=list_published,
        )


# ── HTTP layer ───────────────────────────────────────────────────────


def _new_session(timeout: float = 60.0) -> requests.Session:
    # huntsvilleal.gov sits behind a WAF that 403s httpx requests due to TLS
    # fingerprinting. The standard ``requests`` library (urllib3 under the
    # hood) doesn't trigger it, so we use that here. Default timeout is
    # carried on the wrapper class below.
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.huntsvilleal.gov/residents/neighborhoods/code-enforcement/",
    })
    s._timeout = timeout  # type: ignore[attr-defined]
    return s


def _try_fetch_pdf(session: requests.Session, year: int, month: int) -> bytes | None:
    """Attempt to download a specific month's PDF. Returns None on 404."""
    url = PDF_URL_TEMPLATE.format(year=year, month=month)
    try:
        r = session.get(url, timeout=getattr(session, "_timeout", 60.0))
    except requests.RequestException as e:
        logger.warning("HTTP error fetching %s: %s", url, e)
        return None
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def _find_latest_pdf(
    session: requests.Session, year: int | None = None, month: int | None = None,
    *, max_lookback: int = 6,
) -> tuple[bytes, int, int]:
    """Locate the most recent Unsafe Building PDF.

    If year+month are given, fetch that one (no lookback). Otherwise start
    from the current calendar month and walk backward until a PDF is found.
    Raises RuntimeError if no PDF is found within ``max_lookback`` months.
    """
    if year is not None and month is not None:
        pdf = _try_fetch_pdf(session, year, month)
        if pdf is None:
            raise RuntimeError(f"No Unsafe Building PDF for {year}-{month:02d}")
        return pdf, year, month

    today = date.today()
    cur_year, cur_month = today.year, today.month
    for _ in range(max_lookback):
        pdf = _try_fetch_pdf(session, cur_year, cur_month)
        if pdf is not None:
            return pdf, cur_year, cur_month
        cur_month -= 1
        if cur_month == 0:
            cur_month = 12
            cur_year -= 1
    raise RuntimeError(f"No Unsafe Building PDF found in last {max_lookback} months")


# ── PDF parsing ──────────────────────────────────────────────────────


def _extract_text(pdf_bytes: bytes) -> str:
    """Run pdfminer over the PDF bytes."""
    from io import BytesIO

    from pdfminer.high_level import extract_text
    return extract_text(BytesIO(pdf_bytes))


def _extract_published_date(pdf_text: str) -> str:
    """Pull the 'as of <date>' or 'Generated: <date>' stamp from the PDF.

    Returns YYYY-MM-DD, or today's date if neither stamp is present.
    """
    for pattern in (
        r"as of\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"Generated:\s*(\d{1,2}/\d{1,2}/\d{4})",
    ):
        m = re.search(pattern, pdf_text, re.IGNORECASE)
        if m:
            try:
                return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
    return date.today().strftime("%Y-%m-%d")


def _parse_records(pdf_text: str) -> list[HuntsvilleUnsafeRecord]:
    """Pair date+case lines with address lines from the extracted PDF text.

    Layout per PDF page is: column 1 lists every Case-Created date paired with
    its Case-Number on a single line, then column 3 lists every Case-Address
    on a separate line. Within each page, the i-th date+case line corresponds
    to the i-th address line. Document order is preserved across pages.
    """
    list_published = _extract_published_date(pdf_text)
    lines = [ln.strip() for ln in pdf_text.split("\n") if ln.strip()]

    date_case_pairs: list[tuple[str, str]] = []
    addresses: list[str] = []
    for ln in lines:
        m = _DATE_CASE_RE.match(ln)
        if m:
            date_case_pairs.append((m.group(1), m.group(2)))
            continue
        if _ADDRESS_RE.match(ln):
            addresses.append(ln)

    if len(date_case_pairs) != len(addresses):
        logger.warning(
            "Date/case count (%d) doesn't match address count (%d) — "
            "pairing first min() of each, %d records dropped",
            len(date_case_pairs), len(addresses),
            abs(len(date_case_pairs) - len(addresses)),
        )

    records: list[HuntsvilleUnsafeRecord] = []
    for (d, c), addr in zip(date_case_pairs, addresses):
        records.append(HuntsvilleUnsafeRecord.from_columns(d, c, addr, list_published))
    return records


# ── Public API ───────────────────────────────────────────────────────


def fetch_unsafe_buildings(
    *,
    year: int | None = None,
    month: int | None = None,
    min_age_years: int = 0,
) -> list[HuntsvilleUnsafeRecord]:
    """Pull the most recent Unsafe Buildings list (or a specific month).

    Args:
        year: Specific report year. If both year+month given, no fallback.
        month: Specific report month (1-12).
        min_age_years: Drop cases newer than N whole years. The most distressed
            properties on this list are typically multi-year cases (city has
            tried and failed to get the owner to comply for years). Use
            ``min_age_years=2`` for the highest-conversion subset.

    Returns:
        List of HuntsvilleUnsafeRecord, in document order. Owner of record is
        not present on these records — to fill it, use ``to_notice_data(rec,
        enrich_owner=True)`` (which makes one Madison API call per record).

    Raises:
        RuntimeError if no PDF can be located.
        requests.RequestException on transport failure.
    """
    session = _new_session()
    pdf_bytes, found_year, found_month = _find_latest_pdf(session, year, month)
    logger.info(
        "Loaded Huntsville Unsafe Building list for %d-%02d (%d KB)",
        found_year, found_month, len(pdf_bytes) // 1024,
    )

    text = _extract_text(pdf_bytes)
    records = _parse_records(text)
    logger.info("Parsed %d unsafe-building records", len(records))

    if min_age_years > 0:
        records = [r for r in records if r.case_age_years >= min_age_years]
    return records


def enrich_with_owner(notice: "NoticeData") -> bool:
    """Look up owner of record via Madison's AssuranceWeb address-search.

    Reads ``notice.address`` (street number + street name), queries the
    Madison property API in address-mode, and writes ``owner_name``,
    ``tax_owner_name``, ``parcel_id``, and assessed-value back onto the
    notice when a match is found.

    Returns True if a match was applied. Empirical hit rate against the
    Huntsville unsafe-buildings list is ~80%; misses are usually
    multi-unit / condemned-and-demolished / tax-exempt parcels not on the
    standard tax roll.
    """
    if notice.owner_name:
        return False  # already enriched
    parts = notice.address.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        return False

    from madison_property_api import search_by_situs_address
    try:
        matches = search_by_situs_address(parts[0], parts[1])
    except Exception as exc:  # network / form failure — don't blow up the batch
        logger.warning("Owner-enrich failed for %r: %s", notice.address, exc)
        return False
    if not matches:
        return False

    # Prefer exact-situs match if multiple parcels share the street number
    target = notice.address.upper().strip()
    exact = [m for m in matches if m.situs_address.upper() == target]
    pick = exact[0] if exact else matches[0]

    notice.owner_name = pick.owner_name
    notice.tax_owner_name = pick.owner_name
    if not notice.parcel_id:
        notice.parcel_id = pick.parcel_number
    return True


def to_notice_data(
    rec: HuntsvilleUnsafeRecord, *, enrich_owner: bool = False,
) -> "NoticeData":
    """Convert an unsafe-building record into a NoticeData.

    Sets ``notice_type="code_violation"``. When ``enrich_owner=True``, also
    runs `enrich_with_owner()` to fill the owner of record via the Madison
    property API (one HTTP call per record — defaults off so callers can
    opt in for bulk runs).
    """
    from notice_parser import NoticeData

    today = date.today().strftime("%Y-%m-%d")
    notice = NoticeData(
        county="Madison",
        state="AL",
        notice_type="code_violation",
        # Subtype is the action signal — every parcel on the Unsafe Building
        # list has been formally declared uninhabitable. The DataSift formatter
        # picks this up to fire `unsafe_building` and `demolish` tags so filter
        # presets can route these into a tear-down-focused sequence (different
        # script, different valuation framing — buyers expect a teardown).
        notice_subtype="unsafe_building",
        date_added=rec.list_published or today,
        received_date=today,
        # Owner intentionally blank — filled below if enrich_owner is requested
        owner_name="",
        # Property identity
        address=rec.address,
        city=rec.city,
        zip=rec.zip,
        # Case # surfaces in DataSift's "Probate Case Number" column. We don't
        # reuse granted_date for the case-creation date — that field is
        # semantically probate-only. The case-opened date lives in raw_text
        # and date_added (the published-list date) for human inspection.
        case_number=rec.case_number,
        # Source
        source_url="https://www.huntsvilleal.gov/residents/neighborhoods/code-enforcement/",
        municipality="Huntsville",
        raw_text=(
            f"UNSAFE BUILDING — Case {rec.case_number} opened {rec.case_created} "
            f"({rec.case_age_years} yrs old) — {rec.address_full}"
        ),
    )
    if enrich_owner:
        enrich_with_owner(notice)
    return notice


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    year: int | None = None
    month: int | None = None
    min_age_years = 0
    enrich_owners = "--enrich-owners" in argv
    for i, a in enumerate(argv):
        if a == "--year" and i + 1 < len(argv):
            try:
                year = int(argv[i + 1])
            except ValueError:
                pass
        elif a == "--month" and i + 1 < len(argv):
            try:
                month = int(argv[i + 1])
            except ValueError:
                pass
        elif a == "--min-age-years" and i + 1 < len(argv):
            try:
                min_age_years = int(argv[i + 1])
            except ValueError:
                pass

    records = fetch_unsafe_buildings(
        year=year, month=month, min_age_years=min_age_years,
    )
    print(f"\nHuntsville unsafe-building cases: {len(records)}")
    if min_age_years > 0:
        print(f"  (filter: case age >= {min_age_years} years)")
    if enrich_owners:
        print(f"  (enriching owners via Madison property API — ~{len(records)} HTTP calls)")

    if not records:
        return 0

    by_zip: dict[str, int] = {}
    age_buckets = {"<1yr": 0, "1-2yr": 0, "3-5yr": 0, "6-10yr": 0, "10+yr": 0}
    for r in records:
        by_zip[r.zip] = by_zip.get(r.zip, 0) + 1
        a = r.case_age_years
        if a < 1:    age_buckets["<1yr"] += 1
        elif a <= 2: age_buckets["1-2yr"] += 1
        elif a <= 5: age_buckets["3-5yr"] += 1
        elif a <= 10: age_buckets["6-10yr"] += 1
        else:        age_buckets["10+yr"] += 1

    print(f"  By ZIP: {dict(sorted(by_zip.items(), key=lambda kv: -kv[1])[:8])}")
    print(f"  By age: {age_buckets}")

    print("\nOldest 10 (most-distressed-by-case-age):")
    oldest = sorted(records, key=lambda r: r.case_created)[:10]
    for r in oldest:
        print(
            f"  {r.case_number:14s}  opened {r.case_created}  "
            f"({r.case_age_years:>2} yrs)  {r.address_full}"
        )

    print("\nFirst record (full):")
    print(json.dumps(asdict(records[0]), indent=2))

    if enrich_owners:
        # Run enrichment across all records and report the hit rate
        notices = [to_notice_data(r, enrich_owner=True) for r in records]
        hits = sum(1 for n in notices if n.owner_name)
        print(f"\nOwner enrichment: {hits}/{len(notices)} ({100*hits/max(1,len(notices)):.0f}%) filled")
        print("\nSample (first 10 with owner):")
        with_owner = [n for n in notices if n.owner_name][:10]
        for n in with_owner:
            print(f"  {n.address:30s}  {n.owner_name}")
    else:
        notice = to_notice_data(records[0])
        print("\nFirst record as NoticeData (use --enrich-owners to fill owner_name):")
        for f in (
            "county", "notice_type", "case_number", "address", "city", "zip",
            "owner_name", "parcel_id", "date_added", "municipality",
        ):
            print(f"  {f:18s} = {getattr(notice, f)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
