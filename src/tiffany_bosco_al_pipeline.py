"""Tiffany & Bosco, P.A. — Alabama foreclosure sale schedule adapter.

Tiffany & Bosco (T&B) is the dominant AL non-judicial foreclosure firm
(successor to Sirote & Permutt's mortgage practice — Homewood, AL office
led by Ginny Cochran Rutledge). They publish their entire AL pending
sale schedule as a public HTML page at:

    https://fs.tblaw.com/Sales/PendingSalesAl.aspx

Companion to rubin_lublin_al_pipeline.py — the two portals give us
independent coverage across the top 2 firms handling AL foreclosures.
T&B verified 2026-07-08 as publishing Madison County foreclosure notices
in The Madison County Record (per a T&B/PennyMac notice PDF), so their
portal is the canonical Madison + Marshall foreclosure signal alongside
Rubin Lublin.

Page structure:
- ASP.NET WebForms with __VIEWSTATE (server-rendered on GET — no JS
  needed to see data despite an ``iagree`` cookie check in the client)
- Each property occupies 3 <tr class="itemRow"> rows correlated by an
  index suffix on span IDs (ListView1_lblSaleDate_N, LinkButton1_N,
  lblSaleLoc_N, lblBidAmount_N, lblCanceled_N)
- Address embedded in a JavaScript-onclick GMap.aspx?address=... link
- County name is a bare text cell in the 3rd row

Fields exposed per property:
    - Sale date (may show "future_date (postponed_from)" if rescheduled)
    - T&B file # (e.g. "23-09420", "25-14644-SP-AL", "26-07576-MF-AL")
    - Loan date + original loan amount
    - Property address (street, city, state zip)
    - Sale time window ("Between 11:00 AM and 1:00 PM local time")
    - Place of sale (courthouse location text)
    - County name
    - Opening bid amount
    - Cancellation status

Live 2026-07-08 default-window snapshot: 18 pending AL sales / 4 Jefferson
+ 0 Madison + 0 Marshall (Madison/Marshall inventory varies week-to-week;
the previous session's research pulled 5 Madison + 1 Marshall on 7/2).

Owner name is NOT on the T&B listing — we enrich via county property API
(Jefferson E-Ring / Madison AssuranceWeb / Marshall AssuranceWeb) using
the same address-search helpers Rubin Lublin uses.

CLI:
    python src/tiffany_bosco_al_pipeline.py
    python src/tiffany_bosco_al_pipeline.py --counties Madison,Marshall
    python src/tiffany_bosco_al_pipeline.py --tiers 1,2 --enrich-owner \\
        --output-datasift-csv output/tb_foreclosures.csv
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)

TB_AL_URL = "https://fs.tblaw.com/Sales/PendingSalesAl.aspx"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ID pattern for the ASP.NET ListView items (correlated by trailing index).
_SALE_DATE_ID_RE = re.compile(r"^ListView1_lblSaleDate_(\d+)$")

# Address format from the GMap link's inner text: "street, city, ST zip"
_ADDR_PARTS_RE = re.compile(
    r"^(.*?),\s*([^,]+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$"
)

# Sale date cell may be "8/12/2026(7/8/2026)" when postponed from an
# earlier date — capture the FIRST (currently-scheduled) date.
_SALE_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


@dataclass
class TiffanyBoscoRecord:
    """One property from Tiffany & Bosco's AL pending-sales page."""

    sale_date_raw: str = ""     # "8/12/2026(7/8/2026)" or "7/8/2026"
    sale_date: str = ""         # "2026-08-12" (ISO)
    file_number: str = ""       # "26-07576-MF-AL"
    loan_date: str = ""         # "12/1/2023"
    original_loan_amount: str = ""  # "$116,844.00"
    opening_bid: str = ""       # "$76,380.00 (o)"
    sale_time_window: str = ""  # "Between 11:00 AM and 1:00 PM local time"
    address: str = ""           # "1424 Reese Ave"
    city: str = ""              # "Elba"
    state: str = "AL"
    zipcode: str = ""           # "36323"
    county: str = ""            # "Coffee"
    place_of_sale: str = ""     # Full courthouse location text
    cancelled: bool = False

    # Owner enrichment (populated by enrich_with_owner when --enrich-owner)
    owner_name: str = ""
    parcel_id: str = ""
    assessed_value: str = ""
    is_homestead: bool = False


# ── Fetch + parse ──────────────────────────────────────────────────────


def fetch_tb_al(
    *,
    timeout: float = 30.0,
    url: str = TB_AL_URL,
) -> list[TiffanyBoscoRecord]:
    """Fetch and parse T&B's AL pending sales page.

    Returns every property in the default GET view. The default view is
    NOT restricted by date — it shows all upcoming sales the firm has
    scheduled (verified 2026-07-08 vs an ASP.NET POST-back with
    tbSaleEnd=today+4mo which counter-intuitively returned FEWER records
    for some counties, meaning the POST-back is a date-range FILTER not
    a widening — the default already shows everything unbounded).

    The ``iagree=true`` cookie set here bypasses T&B's client-side terms
    of use redirect. It's a JS-only check — server serves data on GET
    regardless — but we set the cookie defensively in case they add
    server-side enforcement.
    """
    logger.info("Fetching Tiffany & Bosco AL pending sales: %s", url)
    resp = requests.get(
        url,
        headers={"User-Agent": _UA},
        cookies={"iagree": "true"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_html(resp.text)


def _parse_html(html: str) -> list[TiffanyBoscoRecord]:
    soup = BeautifulSoup(html, "html.parser")
    # Each property is anchored by a `ListView1_lblSaleDate_N` span.
    sale_date_spans = soup.find_all("span", id=_SALE_DATE_ID_RE)
    records: list[TiffanyBoscoRecord] = []
    for span in sale_date_spans:
        m = _SALE_DATE_ID_RE.match(span.get("id", ""))
        if not m:
            continue
        idx = m.group(1)
        rec = _parse_single(soup, idx, span)
        if rec:
            records.append(rec)
    return records


def _parse_single(
    soup: BeautifulSoup, idx: str, sale_date_span,
) -> TiffanyBoscoRecord | None:
    rec = TiffanyBoscoRecord()

    # Row A: sale-date's parent <tr> contains file#, loan date, orig amount, bid
    rec.sale_date_raw = sale_date_span.get_text(strip=True)
    m = _SALE_DATE_RE.search(rec.sale_date_raw)
    if m:
        try:
            rec.sale_date = datetime.strptime(
                m.group(1), "%m/%d/%Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    tr_a = sale_date_span.find_parent("tr")
    if tr_a:
        tds = tr_a.find_all("td")
        if len(tds) > 1:
            rec.file_number = tds[1].get_text(strip=True)
        if len(tds) > 2:
            rec.loan_date = tds[2].get_text(strip=True)
        if len(tds) > 4:
            rec.original_loan_amount = tds[4].get_text(strip=True)

    # Bid
    bid_span = soup.find("span", id=f"ListView1_lblBidAmount_{idx}")
    if bid_span:
        rec.opening_bid = bid_span.get_text(strip=True)

    # Sale time window
    time_span = soup.find("span", id=f"ListView1_Label1_{idx}")
    if time_span:
        rec.sale_time_window = time_span.get_text(strip=True)

    # Address from LinkButton1_{idx}
    addr_link = soup.find("a", id=f"ListView1_LinkButton1_{idx}")
    if addr_link:
        raw_addr = addr_link.get_text(strip=True)
        parts = _ADDR_PARTS_RE.match(raw_addr)
        if parts:
            rec.address = parts.group(1).strip()
            rec.city = parts.group(2).strip()
            rec.state = parts.group(3).strip()
            rec.zipcode = parts.group(4).strip()[:5]  # canonicalize 5-digit
        else:
            # Fallback — keep the raw string as address, leave city/zip empty
            rec.address = raw_addr

    # Row C: place of sale + county
    sale_loc = soup.find("span", id=f"ListView1_lblSaleLoc_{idx}")
    if sale_loc:
        rec.place_of_sale = sale_loc.get_text(strip=True)
        tr_c = sale_loc.find_parent("tr")
        if tr_c:
            tds_c = tr_c.find_all("td")
            if len(tds_c) >= 2:
                rec.county = tds_c[1].get_text(strip=True)

    # Cancellation status
    cancel_span = soup.find("span", id=f"ListView1_lblCanceled_{idx}")
    if cancel_span and cancel_span.get_text(strip=True):
        rec.cancelled = True

    return rec


# ── Filters ────────────────────────────────────────────────────────────


def filter_records(
    records: list[TiffanyBoscoRecord],
    *,
    counties: tuple[str, ...] | None = None,
    tiers: tuple[int, ...] | None = None,
    include_cancelled: bool = False,
) -> list[TiffanyBoscoRecord]:
    """Filter records by county, ZIP tier, and cancellation status.

    ``counties`` — set to ("Jefferson", "Madison", "Marshall") for the
    default SiftStack scope. Case-insensitive match.

    ``tiers`` — set to (1, 2) to keep only Tier-1 + Tier-2 ZIPs per
    ``target_zips.zip_tier_county``. Records without ZIP drop.

    ``include_cancelled`` — by default cancelled sales are dropped (the
    trustee's sale isn't happening; not actionable). Set True to keep
    them for audit purposes.
    """
    result = list(records)

    if not include_cancelled:
        result = [r for r in result if not r.cancelled]

    if counties:
        wanted = {c.lower() for c in counties}
        result = [r for r in result if (r.county or "").lower() in wanted]

    if tiers is not None:
        from target_zips import zip_tier_county
        tier_set = set(tiers)
        kept: list[TiffanyBoscoRecord] = []
        for r in result:
            zip5 = (r.zipcode or "").strip()[:5]
            if not zip5:
                continue
            t, _ = zip_tier_county(zip5)
            if t in tier_set:
                kept.append(r)
        result = kept

    return result


# ── Owner enrichment (via county property APIs) ────────────────────────


def enrich_with_owner(rec: TiffanyBoscoRecord) -> bool:
    """Fill owner_name/parcel_id/assessed_value via county property API.

    Same waterfall as rubin_lublin_al_pipeline.enrich_with_owner — routes
    to Jefferson E-Ring / Madison AssuranceWeb / Marshall AssuranceWeb by
    ``rec.county``. Returns True if owner_name was filled.
    """
    county = (rec.county or "").lower()
    if not rec.address:
        return False
    try:
        if county == "jefferson":
            from jefferson_property_api import search_by_situs_address
            matches = search_by_situs_address(rec.address)
        elif county == "madison":
            from madison_property_api import search_by_situs_address
            house_num, street = _split_house_num(rec.address)
            matches = search_by_situs_address(house_num, street) if house_num else []
        elif county == "marshall":
            from marshall_property_api import search_by_situs_address
            house_num, street = _split_house_num(rec.address)
            matches = search_by_situs_address(house_num, street) if house_num else []
        else:
            return False
    except Exception as e:
        logger.debug("Owner enrichment failed for %r (%s): %s",
                     rec.address, county, e)
        return False

    if not matches:
        return False
    m = matches[0]
    rec.owner_name = getattr(m, "owner_name", "") or ""
    rec.parcel_id = getattr(m, "parcel_number", "") or getattr(m, "parcel_id", "") or ""
    total_value = getattr(m, "total_value", "") or getattr(m, "assessed_value", "")
    rec.assessed_value = str(total_value) if total_value else ""
    rec.is_homestead = bool(
        getattr(m, "is_homestead", False) or getattr(m, "is_buildable", False)
    )
    return bool(rec.owner_name)


def _split_house_num(address: str) -> tuple[str, str]:
    m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", address or "")
    if not m:
        return ("", address or "")
    return (m.group(1), m.group(2))


# ── NoticeData conversion ──────────────────────────────────────────────


def to_notice_data(rec: TiffanyBoscoRecord) -> "NoticeData":
    """Convert a T&B record to a NoticeData for the standard downstream."""
    from notice_parser import NoticeData
    n = NoticeData()
    n.notice_type = "foreclosure"
    n.county = (rec.county or "").strip().title()  # Madison / Marshall / Jefferson
    n.state = "AL"
    n.address = rec.address
    n.city = rec.city
    n.zip = rec.zipcode
    n.auction_date = _mmddyyyy(rec.sale_date)
    n.date_added = date.today().strftime("%Y-%m-%d")
    n.received_date = n.date_added
    n.source_url = TB_AL_URL
    n.case_number = rec.file_number
    n.trustee = "Tiffany & Bosco, P.A."
    n.raw_text = (
        f"Foreclosure | Auction: {n.auction_date} | Municipality: {rec.city} | "
        f"Trustee: Tiffany & Bosco, P.A. | File#: {rec.file_number} | "
        f"Original Loan: {rec.original_loan_amount} | "
        f"Opening Bid: {rec.opening_bid} | Sale Location: {rec.place_of_sale} | "
        f"Source: Tiffany & Bosco AL portal"
    )
    if rec.owner_name:
        n.owner_name = rec.owner_name
        n.tax_owner_name = rec.owner_name
    if rec.parcel_id:
        n.parcel_id = rec.parcel_id
    if rec.assessed_value:
        n.assessed_value = rec.assessed_value
    if rec.is_homestead:
        n.is_homestead = "Y"
    return n


def _mmddyyyy(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%-m/%-d/%Y")
    except ValueError:
        return iso


# ── Orchestrator ───────────────────────────────────────────────────────


def fetch_all(
    *,
    counties: tuple[str, ...] = ("Jefferson", "Madison", "Marshall"),
    tiers: tuple[int, ...] | None = (1, 2),
    enrich_owner: bool = False,
    include_cancelled: bool = False,
) -> list["NoticeData"]:
    """One-shot fetch + filter + enrich + convert to NoticeData."""
    records = fetch_tb_al()
    logger.info("T&B AL: %d total records fetched", len(records))
    records = filter_records(
        records,
        counties=counties,
        tiers=tiers,
        include_cancelled=include_cancelled,
    )
    logger.info(
        "T&B AL: %d records after county/tier/cancel filter "
        "(counties=%s, tiers=%s, include_cancelled=%s)",
        len(records), counties, tiers, include_cancelled,
    )

    if enrich_owner:
        hits = 0
        for r in records:
            if enrich_with_owner(r):
                hits += 1
        logger.info("T&B AL: owner enrichment filled %d/%d records",
                    hits, len(records))

        # Fallback: fill remaining empty owner_names from the APN owner
        # cache (see src/owner_cache.py for full motivation). Catches
        # properties whose tax-roll owner name diverges from (or is missing
        # on) the county API but which have appeared in APN in the last
        # 90 days with a mortgagor name extracted from the notice body.
        try:
            import owner_cache
            filled = owner_cache.fill_missing_owners(records)
            if filled:
                logger.info("T&B AL: APN owner cache filled +%d additional "
                            "records (%d/%d total)",
                            filled, hits + filled, len(records))
        except Exception as e:
            logger.debug("Owner cache fallback skipped: %s", e)

    return [to_notice_data(r) for r in records]


# ── CLI ────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tiffany_bosco_al_pipeline",
        description="Tiffany & Bosco AL foreclosure schedule → NoticeData / CSV.",
    )
    p.add_argument(
        "--counties", default="Jefferson,Madison,Marshall",
        help="Comma-separated counties (default: Jefferson,Madison,Marshall).",
    )
    p.add_argument(
        "--tiers", default="1,2",
        help="Comma-separated ZIP tiers (default '1,2', 'all' disables).",
    )
    p.add_argument(
        "--enrich-owner", action="store_true",
        help="Enrich owner_name via county property API address-search "
             "(~0.3s/record).",
    )
    p.add_argument(
        "--skip-trace", action="store_true",
        help="Run Tracerfy batch skip-trace on records with owner_name "
             "(fills Phone 1-9 / Email 1-5) + Trestle phone scoring for "
             "DataSift dial-priority tiers. Off by default; enable for "
             "daily-ops. ~$0.02/contact.",
    )
    p.add_argument(
        "--include-cancelled", action="store_true",
        help="Keep cancelled sales (default: dropped as not-actionable).",
    )
    p.add_argument(
        "--output-csv", type=Path, default=None,
        help="Write standard Sift-format CSV to this path.",
    )
    p.add_argument(
        "--output-datasift-csv", type=Path, default=None,
        help="Write DataSift-format CSV (80 cols) to this path.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Print raw records as JSON (skip NoticeData conversion).",
    )
    return p


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = _build_argparser().parse_args(argv)

    counties = tuple(c.strip() for c in args.counties.split(",") if c.strip())
    tiers_arg = (args.tiers or "").lower()
    tiers: tuple[int, ...] | None
    if tiers_arg in ("", "all"):
        tiers = None
    else:
        tiers = tuple(int(t) for t in args.tiers.split(",") if t.strip().isdigit())

    if args.json:
        import json
        records = fetch_tb_al()
        records = filter_records(
            records, counties=counties, tiers=tiers,
            include_cancelled=args.include_cancelled,
        )
        if args.enrich_owner:
            for r in records:
                enrich_with_owner(r)
        print(json.dumps([asdict(r) for r in records], indent=2))
        return 0

    notices = fetch_all(
        counties=counties,
        tiers=tiers,
        enrich_owner=args.enrich_owner,
        include_cancelled=args.include_cancelled,
    )

    print(f"\n=== Tiffany & Bosco AL — {len(notices)} notice(s) ===")
    for n in notices:
        owner = n.owner_name or "(no owner)"
        print(
            f"  {n.county:10s} {n.zip:5s} {n.address:35s} sale={n.auction_date}  "
            f"file={n.case_number}  owner={owner}"
        )

    # Smarty USPS standardization — normalizes address per USPS CASS,
    # fills lat/lng + plus4_code + dpv_match_code, validates deliverability.
    # Same call the main.py daily pipeline uses via enrichment_pipeline.
    if notices:
        try:
            import config
            if config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
                from address_standardizer import standardize_addresses
                standardize_addresses(
                    notices,
                    config.SMARTY_AUTH_ID,
                    config.SMARTY_AUTH_TOKEN,
                )
                confirmed = sum(
                    1 for n in notices if getattr(n, "dpv_match_code", "") == "Y"
                )
                logger.info(
                    "Smarty standardization: %d/%d DPV-confirmed",
                    confirmed, len(notices),
                )
            else:
                logger.info("Smarty standardization skipped — no SMARTY_AUTH_ID/TOKEN.")
        except Exception as e:
            logger.warning(
                "Smarty standardization failed (continuing without): %s", e,
            )

    # Tracerfy skip-trace + Trestle phone scoring — same gap fix as
    # code_violation_pipeline (f100e83). Without this, Phone 1-9 /
    # Email 1-5 columns stay empty on uploaded records.
    phone_tiers: dict | None = None
    if args.skip_trace and notices:
        traceable = [n for n in notices if (n.owner_name or "").strip()]
        if traceable:
            try:
                import tracerfy_skip_tracer
                stats = tracerfy_skip_tracer.batch_skip_trace(traceable)
                logger.info(
                    "Skip-trace stats: submitted=%d matched=%d phones=%d "
                    "emails=%d cost=$%.2f",
                    stats.get("submitted", 0), stats.get("matched", 0),
                    stats.get("phones_found", 0),
                    stats.get("emails_found", 0),
                    stats.get("cost", 0.0),
                )
            except Exception as e:
                logger.warning(
                    "Skip-trace failed (continuing without phones): %s", e,
                )
            try:
                from phone_validator import score_phones_for_pipeline
                phone_tiers = score_phones_for_pipeline(notices)
            except Exception as e:
                logger.warning(
                    "Trestle scoring failed (continuing without tiers): %s", e,
                )
        else:
            logger.info(
                "Skip-trace requested but no notices have owner_name — "
                "consider running with --enrich-owner first.",
            )

    if args.output_csv:
        from data_formatter import write_csv
        path = write_csv(notices, str(args.output_csv))
        print(f"\nWrote Sift CSV: {path}")
    if args.output_datasift_csv:
        from datasift_formatter import write_datasift_csv
        path = write_datasift_csv(
            notices, str(args.output_datasift_csv),
            phone_tiers=phone_tiers,
        )
        print(f"Wrote DataSift CSV: {path}")

    return 0 if notices else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
