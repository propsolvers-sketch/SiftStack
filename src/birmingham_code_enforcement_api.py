"""City of Birmingham, AL — Accela Citizen Access code-enforcement adapter.

Scrapes Birmingham's public Accela portal at:
  https://aca-prod.accela.com/BIRMINGHAM/Cap/CapHome.aspx?module=Enforcement

This is the **early-distress** companion to the Huntsville Unsafe Buildings
list (huntsville_unsafe_buildings_api.py). Where Huntsville surfaces the
end-of-pipeline "we're going to demolish this" cases, Birmingham's Accela
portal exposes the full enforcement funnel:

  Condemnation              — formal teardowns (matches unsafe_building tier)
  Housing Enforcement       — IPMC code violations: roof/siding/structure
  Inoperable Vehicles       — junk cars, abandoned vehicles
  Environmental Enforcement — tall grass, weeds, junk, debris
  Zoning Enforcement        — illegal use, setback violations, etc.
  Environmental Batch Record— bulk env. enforcement
                                (lower individual-record value)

Every category EXCEPT Condemnation is "early distress" — owner is still in
the property but slipping. These signal financial / motivational issues
months-to-years before properties hit foreclosure or unsafe-building lists,
giving wholesale operators a longer reach-out window.

The adapter requires Playwright because Accela uses ASP.NET WebForms with
__VIEWSTATE postbacks. Search-results are list-only (~6 columns: date,
address, case#, type, description, status); per-case owner name + mailing
address + accumulated fines require a follow-up detail-page click (opt-in
via ``enrich_details=True``).

CLI:
    python src/birmingham_code_enforcement_api.py
    python src/birmingham_code_enforcement_api.py --category housing --days 30
    python src/birmingham_code_enforcement_api.py --enrich-details --max-pages 3
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from notice_parser import NoticeData
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

SEARCH_URL = (
    "https://aca-prod.accela.com/BIRMINGHAM/Cap/CapHome.aspx"
    "?module=Enforcement&TabName=Enforcement"
)

# Accela Record Type values (from the ddlGSPermitType dropdown).
# Each value maps to a friendly category key + a NoticeData notice_subtype.
CATEGORIES: dict[str, tuple[str, str]] = {
    # cli_key:        (accela_value,                                          subtype)
    "condemnation":   ("Enforcement/Condemnation/NA/NA",                      "unsafe_building"),
    "housing":        ("Enforcement/Housing Property Maintenance/NA/NA",      "housing_enforcement"),
    "vehicles":       ("Enforcement/Inoperable Vehicles/NA/NA",               "inoperable_vehicle"),
    "environmental":  ("Enforcement/Environmental/NA/NA",                     "environmental_enforcement"),
    "zoning":         ("Enforcement/Incident/Zoning/Zoning Enforcement",      "zoning_enforcement"),
}


# ── Data model ───────────────────────────────────────────────────────


@dataclass
class BirminghamEnforcementRecord:
    """One Birmingham code-enforcement case."""
    case_number: str             # e.g. "HEN2026-00330"
    case_opened: str             # YYYY-MM-DD
    address: str                 # Situs address (unparsed string)
    category: str                # CLI key: housing | vehicles | environmental | zoning | condemnation
    notice_subtype: str          # NoticeData subtype value (housing_enforcement, etc.)
    description: str             # IPMC violation description
    status: str                  # "Violation Verified" / "Open" / "Closed" / etc.
    # Detail-page fields — populated only when enrich_details=True
    owner_name: str = ""
    owner_address: str = ""      # Owner's mailing address from Accela detail page
    fee_total: float = 0.0       # Total fees / fines assessed (across all line items)
    fee_balance: float = 0.0     # Balance still owed
    detail_url: str = ""

    # Convenience for serialization
    extra: dict = field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────


def _parse_date(s: str) -> str:
    """Convert MM/DD/YYYY → YYYY-MM-DD; empty if bad."""
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _money(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    s = s.replace("$", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


# Birmingham Accela addresses look like "305 OREGON ST, BIRMINGHAM AL 35224"
# or "1124 SW 16TH ST SW, BHAM AL 35211" — comma between street and city,
# spaces between city/state/zip. Split into clean components so the city
# normalizer can convert BHAM → Birmingham.
_BIRM_ADDR_RE = re.compile(
    r"^(?P<street>.+?)\s*,\s*(?P<city>[A-Za-z][A-Za-z .'\-]*?)\s+"
    r"(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


def _parse_birmingham_address(raw: str) -> tuple[str, str, str, str]:
    """Split a full Birmingham Accela address into (street, city, state, zip).

    Returns (raw_string, "", "", "") if the regex doesn't match — callers
    should fall back to the raw string as the address.
    """
    if not raw:
        return ("", "", "", "")
    m = _BIRM_ADDR_RE.match(raw.strip())
    if not m:
        return (raw.strip(), "", "", "")
    return (
        m.group("street").strip(),
        m.group("city").strip(),
        m.group("state").strip(),
        m.group("zip").strip(),
    )


# ── Search + pagination ─────────────────────────────────────────────


async def _submit_search(
    page: "Page", category_value: str, start_date: date, end_date: date,
) -> int:
    """Fill the search form and click submit. Returns total result count (or 0)."""
    await page.fill(
        "#ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate",
        start_date.strftime("%m/%d/%Y"),
    )
    await page.fill(
        "#ctl00_PlaceHolderMain_generalSearchForm_txtGSEndDate",
        end_date.strftime("%m/%d/%Y"),
    )
    await page.select_option(
        "#ctl00_PlaceHolderMain_generalSearchForm_ddlGSPermitType",
        value=category_value,
    )
    await page.wait_for_timeout(400)
    await page.click("#ctl00_PlaceHolderMain_btnNewSearch")
    # Postback isn't a navigation; just wait for the results grid to swap in
    await page.wait_for_timeout(7000)

    # Pull "Showing X-Y of Z" or "no records" message
    info = await page.evaluate("""() => {
      const txt = document.body.innerText;
      const m = txt.match(/Showing\\s+\\d+-\\d+\\s+of\\s+([\\d+]+)/i);
      if (m) return parseInt(m[1].replace('+', ''), 10);
      if (/no records?\\s+found/i.test(txt)) return 0;
      return -1;  // unknown
    }""")
    return int(info or -1)


async def _extract_rows(page: "Page", category_key: str, subtype: str) -> list[BirminghamEnforcementRecord]:
    """Extract case rows from the current Accela results page.

    Accela's data rows have class ``ACA_TabRow_Odd`` / ``ACA_TabRow_Even``.
    Cell layout (live, with checkbox in column 0):
       [0] checkbox  [1] date  [2] address  [3] case#  [4] type
       [5] description  [6] status  [7] empty  [8] address(dup)
    """
    rows = await page.evaluate("""() => {
      const grid = document.querySelector('table[id*="gdvPermitList"]');
      if (!grid) return [];
      return Array.from(grid.querySelectorAll('tr.ACA_TabRow_Odd, tr.ACA_TabRow_Even'))
        .map(tr => Array.from(tr.querySelectorAll('td'))
          .map(td => td.textContent.replace(/\\s+/g, ' ').trim()));
    }""")

    records: list[BirminghamEnforcementRecord] = []
    for cells in rows:
        if len(cells) < 6:
            continue
        case_opened = _parse_date(cells[1])
        address = cells[2] if cells[2] else (cells[8] if len(cells) > 8 else "")
        case_number = cells[3]
        description = cells[5] if len(cells) > 5 else ""
        status = cells[6] if len(cells) > 6 else ""
        # Skip rows whose case-number column doesn't match Accela's format
        if not case_number or not re.match(r"^[A-Z]{2,4}\d{4}-\d+", case_number):
            continue
        records.append(
            BirminghamEnforcementRecord(
                case_number=case_number,
                case_opened=case_opened,
                address=address,
                category=category_key,
                notice_subtype=subtype,
                description=description,
                status=status,
            )
        )
    return records


async def _next_page(page: "Page") -> bool:
    """Click the 'Next >' pagination link if available; return True on success."""
    next_link = await page.query_selector("a:has-text('Next >')")
    if not next_link:
        return False
    try:
        await next_link.click()
        await page.wait_for_timeout(5000)
        return True
    except Exception:
        return False


# ── Detail-page enrichment ─────────────────────────────────────────


async def _enrich_record_detail(page: "Page", rec: BirminghamEnforcementRecord) -> None:
    """Click into the case-detail page; populate owner name, mailing address, fees.

    Returns the page to the search-results state by clicking 'Back' so the
    caller can iterate through the next case. Defensive — failures don't
    propagate; the record stays partially populated.
    """
    link = await page.query_selector(f"a:has-text('{rec.case_number}')")
    if not link:
        return
    try:
        await link.click()
        await page.wait_for_timeout(3500)
    except Exception:
        return
    rec.detail_url = page.url

    # Owner name + mailing address — appear in the "Record Details" section
    body = await page.inner_text("body")
    owner_match = re.search(
        r"Owner:\s*([\w\s&'.\-,*/]+?)\s*\n([\d\w\s.,'#\-]+?\b(?:AL|ALABAMA)\s+\d{5})",
        body, re.IGNORECASE,
    )
    if owner_match:
        rec.owner_name = re.sub(r"\s+", " ", owner_match.group(1)).strip()
        rec.owner_address = re.sub(r"\s+", " ", owner_match.group(2)).strip()

    # Fees — Accela case-detail has a "Fees" or "Total Fees" line
    for fee_pattern in (
        r"Total\s+Fees?:?\s*\$?([\d,]+\.\d{2})",
        r"Fee\s+Total:?\s*\$?([\d,]+\.\d{2})",
        r"Amount\s+Due:?\s*\$?([\d,]+\.\d{2})",
    ):
        m = re.search(fee_pattern, body, re.IGNORECASE)
        if m:
            rec.fee_total = _money(m.group(1))
            break
    bal_match = re.search(r"Balance:?\s*\$?([\d,]+\.\d{2})", body, re.IGNORECASE)
    if bal_match:
        rec.fee_balance = _money(bal_match.group(1))

    # Navigate back to search results
    try:
        # Accela usually has a "Back" link or we can use browser back
        await page.go_back(wait_until="domcontentloaded", timeout=10_000)
        await page.wait_for_timeout(2000)
    except Exception:
        pass


# ── Public API (async core, sync façade) ────────────────────────────


async def _fetch_async(
    *,
    categories: list[str],
    days_back: int,
    max_pages: int,
    enrich_details: bool,
    headless: bool,
) -> list[BirminghamEnforcementRecord]:
    end = date.today()
    start = end - timedelta(days=days_back)
    all_records: list[BirminghamEnforcementRecord] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        for cat_key in categories:
            if cat_key not in CATEGORIES:
                logger.warning("Skipping unknown category: %r", cat_key)
                continue
            cat_value, subtype = CATEGORIES[cat_key]
            logger.info("Searching Birmingham %s (last %d days)…", cat_key, days_back)

            try:
                await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(2500)
                total = await _submit_search(page, cat_value, start, end)
            except Exception as e:
                logger.warning("Search failed for %s: %s", cat_key, e)
                continue
            logger.info("  %s — total results: %s", cat_key, total)

            page_count = 0
            cat_records: list[BirminghamEnforcementRecord] = []
            while True:
                rows = await _extract_rows(page, cat_key, subtype)
                cat_records.extend(rows)
                page_count += 1
                if page_count >= max_pages:
                    break
                advanced = await _next_page(page)
                if not advanced:
                    break

            logger.info("  %s — collected %d records across %d pages", cat_key, len(cat_records), page_count)
            all_records.extend(cat_records)

            if enrich_details and cat_records:
                # Re-run the search to land on page 1 of results, then iterate
                # detail clicks. This is sequential by design — Accela uses
                # __VIEWSTATE so we can't open detail tabs in parallel.
                logger.info("  %s — enriching %d cases with detail-page data…", cat_key, len(cat_records))
                await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(2000)
                await _submit_search(page, cat_value, start, end)
                for rec in cat_records:
                    await _enrich_record_detail(page, rec)

        await browser.close()
    return all_records


def fetch_enforcement_cases(
    *,
    categories: list[str] | None = None,
    days_back: int = 30,
    max_pages: int = 5,
    enrich_details: bool = False,
    headless: bool = True,
) -> list[BirminghamEnforcementRecord]:
    """Pull Birmingham code-enforcement cases.

    Args:
        categories: List of CLI keys from CATEGORIES (default: housing, vehicles,
            environmental, zoning, condemnation — i.e. all except batch records).
        days_back: Days of history to pull (default 30).
        max_pages: Per-category pagination cap (default 5 = ~50 cases each at
            10 per page). Bump for backfills.
        enrich_details: When True, click into each case detail page to fetch
            owner_name, owner_address, fee_total, fee_balance. Adds ~3 seconds
            per case — defaults False so daily runs stay fast.
        headless: Set False for debugging (visible browser).

    Returns:
        List of BirminghamEnforcementRecord across all requested categories.
    """
    if categories is None:
        categories = ["housing", "vehicles", "environmental", "zoning", "condemnation"]
    return asyncio.run(_fetch_async(
        categories=categories,
        days_back=days_back,
        max_pages=max_pages,
        enrich_details=enrich_details,
        headless=headless,
    ))


def enrich_with_owner(notice: "NoticeData") -> bool:
    """Look up owner of record via Jefferson's E-Ring address-search.

    Mirrors the Huntsville → Madison enrichment pattern (Phase 3). Reads
    ``notice.address`` (street part, after ``_parse_birmingham_address``),
    queries the Jefferson property API in property-address mode, and writes
    ``owner_name``, ``tax_owner_name``, ``parcel_id``, and ``assessed_value``
    onto the notice when a match is found.

    Returns True if a match was applied. Empirical hit rate against
    Birmingham code-violation records is ~80%; misses are usually
    tax-exempt parcels or addresses recorded differently in the assessor's
    index than in the city's Accela case database.

    Skips the lookup if ``notice.owner_name`` is already populated (e.g.
    via a prior Accela detail-page enrichment).
    """
    if notice.owner_name:
        return False
    if not notice.address or not notice.address.strip():
        return False

    from jefferson_property_api import search_by_situs_address
    try:
        matches = search_by_situs_address(notice.address)
    except Exception as exc:  # network / form failure — don't blow up the batch
        logger.warning("Owner-enrich failed for %r: %s", notice.address, exc)
        return False
    if not matches:
        return False

    # Prefer exact-situs match if multiple parcels share a street prefix
    target = (notice.address or "").upper().strip()
    exact = [m for m in matches if m.situs_address.upper() == target]
    pick = exact[0] if exact else matches[0]

    notice.owner_name = pick.owner_name
    notice.tax_owner_name = pick.owner_name
    if not notice.parcel_id:
        notice.parcel_id = pick.parcel_number
    if not notice.assessed_value and pick.total_value > 0:
        notice.assessed_value = f"{pick.total_value:.0f}"
    return True


def to_notice_data(
    rec: BirminghamEnforcementRecord, *, enrich_owner: bool = False,
) -> "NoticeData":
    """Convert a Birmingham enforcement case into NoticeData.

    Sets ``notice_type="code_violation"`` and ``notice_subtype`` to the
    category-specific value (e.g. "housing_enforcement"). The DataSift formatter
    uses notice_subtype to fire fine-grained tags for filter presets.

    Address parsing: Accela mixes "BIRMINGHAM" and "BHAM" in the city portion
    of the address string. ``_parse_birmingham_address`` splits the components
    and ``normalize_jefferson_city`` converts both to "Birmingham" so the
    DataSift Property City column never shows the abbreviation.

    Args:
        rec: The Birmingham enforcement case.
        enrich_owner: When True, runs ``enrich_with_owner()`` to fill the
            tax-roll owner of record via the Jefferson property API. One
            HTTP call per record (~0.5s) — defaults False so bulk pulls
            stay free of API calls.
    """
    from notice_parser import NoticeData
    from jefferson_property_api import normalize_jefferson_city

    street, city, state, zip_code = _parse_birmingham_address(rec.address)
    city_normalized = normalize_jefferson_city(city) if city else ""

    today = date.today().strftime("%Y-%m-%d")
    notice = NoticeData(
        county="Jefferson",
        state="AL",
        notice_type="code_violation",
        notice_subtype=rec.notice_subtype,
        date_added=rec.case_opened or today,
        received_date=today,
        owner_name=rec.owner_name,
        address=street or rec.address,
        city=city_normalized,
        zip=zip_code,
        municipality="Birmingham",
        # Mailing address (where the city sends violation notices) — only filled
        # when enrich_details=True. Fallback to situs.
        owner_street=rec.owner_address or "",
        # Case # → "Probate Case Number" column (generic case-id slot)
        case_number=rec.case_number,
        # Fees → tax_delinquent_amount slot is the closest existing field for
        # a money-owed-on-property amount. The DataSift formatter routes it to
        # "Tax Deliquent Value", which is misleading for code-violation fines
        # but the column is conceptually "outstanding municipal balance" so
        # filter-preset users can still find these via tax_high_exposure tags.
        tax_delinquent_amount=f"{rec.fee_total:.2f}" if rec.fee_total > 0 else "",
        source_url=rec.detail_url or SEARCH_URL,
        raw_text=(
            f"BIRMINGHAM CODE ENFORCEMENT — {rec.notice_subtype.replace('_', ' ').upper()} — "
            f"Case {rec.case_number} opened {rec.case_opened} — "
            f"{rec.address} — {rec.description}"
            f"{f' — Status: {rec.status}' if rec.status else ''}"
            f"{f' — Total fees: ${rec.fee_total:,.2f}' if rec.fee_total > 0 else ''}"
        ),
    )
    if enrich_owner:
        enrich_with_owner(notice)
    return notice


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    categories: list[str] | None = None
    days_back = 30
    max_pages = 5
    enrich_details = "--enrich-details" in argv
    enrich_owner = "--enrich-owner" in argv
    headless = "--show" not in argv

    for i, a in enumerate(argv):
        if a == "--category" and i + 1 < len(argv):
            cats = [c.strip().lower() for c in argv[i + 1].split(",") if c.strip()]
            categories = cats
        elif a == "--days" and i + 1 < len(argv):
            try:
                days_back = int(argv[i + 1])
            except ValueError:
                pass
        elif a == "--max-pages" and i + 1 < len(argv):
            try:
                max_pages = int(argv[i + 1])
            except ValueError:
                pass

    records = fetch_enforcement_cases(
        categories=categories,
        days_back=days_back,
        max_pages=max_pages,
        enrich_details=enrich_details,
        headless=headless,
    )
    print(f"\nBirmingham enforcement cases: {len(records)}")
    if not records:
        return 0

    # Per-category breakdown
    by_cat: dict[str, int] = {}
    for r in records:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1
    print(f"  By category: {by_cat}")

    if enrich_details:
        with_acc_owner = sum(1 for r in records if r.owner_name)
        with_fee = sum(1 for r in records if r.fee_total > 0)
        print(f"  Accela detail enriched: {with_acc_owner}/{len(records)}")
        print(f"  With fees:              {with_fee}/{len(records)}")

    # Owner enrichment via Jefferson tax-roll (independent of Accela detail enrichment)
    if enrich_owner:
        print(f"\nEnriching owners via Jefferson property API ({len(records)} HTTP calls)…")
        notices = [to_notice_data(r, enrich_owner=True) for r in records]
        with_tax_owner = sum(1 for n in notices if n.owner_name)
        print(f"  Tax-roll owner filled: {with_tax_owner}/{len(notices)}")

        print("\nSample (first 10 with owner):")
        for n in [x for x in notices if x.owner_name][:10]:
            print(f"  {n.address:30s}  {n.owner_name}")

    print("\nFirst 10 cases:")
    for r in records[:10]:
        owner_part = f" | {r.owner_name}" if r.owner_name else ""
        fee_part = f" | ${r.fee_total:,.2f}" if r.fee_total > 0 else ""
        print(
            f"  {r.case_number}  {r.case_opened}  [{r.category}]  "
            f"{r.address[:40]:40s}{owner_part}{fee_part}"
        )

    print("\nFirst record (full):")
    print(json.dumps(asdict(records[0]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
