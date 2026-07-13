"""Halliday, Watkins & Mann, P.C. — Alabama foreclosure schedule adapter.

HWM is a multi-state trustee firm (SLC HQ + Birmingham AL office) active
in the AL non-judicial foreclosure market. #2 by volume behind Tiffany
& Bosco in our 4-week Jefferson-county capture (10% of records).

Portal: https://halliday-watkins.com/live/AL/bids.htm — pure static HTML,
NO JavaScript required despite earlier research believing it was JS-gated.
Clean 7-column table (File Number | County | State | Property Address |
Sale Date | Sale Time | Opening Bid). Refreshed by HWM as new sales get
scheduled. Live 2026-07-08 snapshot: 71 total AL records / 15 Jefferson
+ 6 Madison + 0 Marshall.

**Distinctive quirk:** HWM does NOT publish ZIP codes on the portal —
addresses are "street, city" format only (e.g. "116 Kings Cross Drive,
Madison"). So the tier-ZIP filter cannot fire pre-enrichment. Instead:
    1. County filter first (Jefferson / Madison / Marshall)
    2. Owner enrichment via county property API — recovers the ZIP from
       the tax roll (Jefferson E-Ring / Madison + Marshall AssuranceWeb)
    3. Tier-ZIP filter on the recovered ZIP
    4. When enrichment misses (~20% rate), fall back to a city-tier
       fallback using the same city → primary-tier-ZIP map that Smarty's
       city-only fallback uses (address_standardizer._CITY_TIER_ZIP_FALLBACK)

This "enrich, then filter" flow is different from Rubin Lublin +
Tiffany & Bosco (which have ZIP up front). It's slower per record (~0.3s
enrichment overhead) but the payoff is real Madison / Marshall records
we'd otherwise miss entirely because HWM strips them of ZIP metadata.

CLI:
    python src/halliday_watkins_al_pipeline.py
    python src/halliday_watkins_al_pipeline.py --counties Madison,Marshall
    python src/halliday_watkins_al_pipeline.py --tiers 1,2 --enrich-owner \\
        --output-datasift-csv output/hwm_foreclosures.csv
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

HWM_AL_URL = "https://halliday-watkins.com/live/AL/bids.htm"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Address format on HWM is "<street>, <city>" (no state, no ZIP). The
# last comma delimits street from city. Occasional records embed a ZIP
# (~8%) but the vast majority don't.
_ZIP_IN_STRING_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


@dataclass
class HallidayWatkinsRecord:
    """One row of Halliday Watkins Mann's AL bids page."""

    file_number: str = ""       # "AL24480"
    county: str = ""            # "Madison"
    state: str = "AL"
    address: str = ""           # "116 Kings Cross Drive"
    city: str = ""              # "Madison"
    zipcode: str = ""           # Usually empty — HWM doesn't publish ZIP
    sale_date_raw: str = ""     # "07/22/2026"
    sale_date: str = ""         # "2026-07-22" (ISO)
    sale_time: str = ""         # "01:00AM" (placeholder — HWM's sale time
                                # column consistently shows "01:00AM" for
                                # all records; the real time is on the
                                # underlying Notice of Sale PDF)
    opening_bid: str = ""       # "0.00" (also placeholder — real bid on PDF)
    sale_status: str = ""       # empty for scheduled sales; may say
                                # "Cancelled", "Postponed", "Sold", etc.

    # Owner enrichment (populated when enrich_with_owner runs). ZIP comes
    # from the tax roll here for records where HWM didn't publish it.
    owner_name: str = ""
    parcel_id: str = ""
    assessed_value: str = ""
    is_homestead: bool = False
    zip_estimated_from_city: bool = False


# ── Fetch + parse ──────────────────────────────────────────────────────


def fetch_hwm_al(
    *,
    timeout: float = 30.0,
    url: str = HWM_AL_URL,
) -> list[HallidayWatkinsRecord]:
    """Fetch and parse the Halliday Watkins Mann AL bids page.

    Returns every AL record in the table. Filter downstream via
    `filter_records()` for county selection.
    """
    logger.info("Fetching Halliday Watkins Mann AL bids: %s", url)
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    return _parse_html(resp.text)


def _parse_html(html: str) -> list[HallidayWatkinsRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[HallidayWatkinsRecord] = []
    for tr in soup.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        # Header row has 8 cells starting with "File Number"; data rows
        # have 7 (no sale_status cell). Skip header and empty rows.
        if len(tds) < 7 or tds[0] == "File Number" or not tds[0]:
            continue
        rec = HallidayWatkinsRecord()
        rec.file_number = tds[0]
        rec.county = tds[1]
        rec.state = tds[2] or "AL"
        raw_addr = tds[3]
        rec.address, rec.city, rec.zipcode = _parse_address(raw_addr)
        rec.sale_date_raw = tds[4]
        rec.sale_date = _iso_date(tds[4])
        rec.sale_time = tds[5]
        rec.opening_bid = tds[6]
        rec.sale_status = tds[7] if len(tds) > 7 else ""
        records.append(rec)
    return records


def _parse_address(raw: str) -> tuple[str, str, str]:
    """'116 Kings Cross Drive, Madison' → ('116 Kings Cross Drive', 'Madison', '').

    HWM strips ZIP from ~92% of records — occasional embedded ZIPs are
    captured when present so we can skip owner enrichment for those.
    """
    if not raw:
        return ("", "", "")
    zip_ = ""
    zm = _ZIP_IN_STRING_RE.search(raw)
    if zm:
        zip_ = zm.group(1)
        raw = raw[:zm.start()].rstrip(", ").strip()
    parts = [p.strip() for p in raw.rsplit(",", 1)]
    if len(parts) == 2:
        return (parts[0], parts[1], zip_)
    return (raw, "", zip_)


def _iso_date(mmddyyyy: str) -> str:
    if not mmddyyyy:
        return ""
    try:
        return datetime.strptime(mmddyyyy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ── Filters ────────────────────────────────────────────────────────────


def filter_by_county(
    records: list[HallidayWatkinsRecord],
    counties: tuple[str, ...],
    *,
    include_cancelled: bool = False,
) -> list[HallidayWatkinsRecord]:
    """Apply the county filter (case-insensitive). Cancelled sales drop
    by default (not actionable — the trustee's sale isn't happening)."""
    wanted = {c.lower() for c in counties}
    out: list[HallidayWatkinsRecord] = []
    for r in records:
        if (r.county or "").lower() not in wanted:
            continue
        status = (r.sale_status or "").lower()
        if not include_cancelled and any(
            k in status for k in ("cancel", "sold", "withdrawn")
        ):
            continue
        out.append(r)
    return out


def filter_by_tier(
    records: list[HallidayWatkinsRecord],
    tiers: tuple[int, ...],
) -> list[HallidayWatkinsRecord]:
    """Keep only records whose ZIP is in the requested tier set.

    Records without a ZIP drop — this filter is meant to fire AFTER
    owner enrichment when HWM's ZIP-stripped records have had their
    ZIP recovered from the tax roll. If enrichment hasn't run yet, most
    HWM records will drop here (HWM publishes only 6/71 records with
    a ZIP directly).
    """
    from target_zips import zip_tier_county
    tier_set = set(tiers)
    out: list[HallidayWatkinsRecord] = []
    for r in records:
        zip5 = (r.zipcode or "").strip()[:5]
        if not zip5:
            continue
        t, _ = zip_tier_county(zip5)
        if t in tier_set:
            out.append(r)
    return out


# ── Owner enrichment (via county property APIs) ────────────────────────


def enrich_with_owner(rec: HallidayWatkinsRecord) -> bool:
    """Fill owner_name + parcel_id + ZIP (from tax roll) via county API.

    Extension over the RL/T&B enrichment: HWM records almost never have
    ZIP on the source, so we ALSO copy the situs_zip from the matched
    property record. When the county API can't find a match, fall back
    to the city-tier centroid map (address_standardizer._CITY_TIER_ZIP_FALLBACK)
    to at least assign a tier-eligible ZIP with the ``zip_estimated_from_city``
    flag set.

    Returns True when owner_name AND zipcode were filled (either from tax
    roll or city fallback). Returns False when both failed.
    """
    county = (rec.county or "").lower()
    if not rec.address:
        return False

    matches: list = []
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
    except Exception as e:
        logger.debug("Owner enrichment API call failed for %r (%s): %s",
                     rec.address, county, e)

    if matches:
        m = matches[0]
        rec.owner_name = getattr(m, "owner_name", "") or ""
        rec.parcel_id = (
            getattr(m, "parcel_number", "") or getattr(m, "parcel_id", "") or ""
        )
        total_value = (
            getattr(m, "total_value", "") or getattr(m, "assessed_value", "")
        )
        rec.assessed_value = str(total_value) if total_value else ""
        rec.is_homestead = bool(
            getattr(m, "is_homestead", False) or getattr(m, "is_buildable", False)
        )
        situs_zip = getattr(m, "situs_zip", "") or ""
        if situs_zip and not rec.zipcode:
            rec.zipcode = situs_zip[:5]

    # Fallback: if we still don't have a ZIP, try city-tier centroid.
    if not rec.zipcode and rec.city:
        try:
            from address_standardizer import _CITY_TIER_ZIP_FALLBACK
            fallback_zip = _CITY_TIER_ZIP_FALLBACK.get(rec.city.upper())
            if fallback_zip:
                rec.zipcode = fallback_zip
                rec.zip_estimated_from_city = True
                logger.debug(
                    "HWM city-tier fallback for %r: %s → %s (estimated)",
                    rec.address, rec.city, fallback_zip,
                )
        except ImportError:
            pass

    return bool(rec.owner_name) or bool(rec.zipcode)


def _split_house_num(address: str) -> tuple[str, str]:
    m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", address or "")
    if not m:
        return ("", address or "")
    return (m.group(1), m.group(2))


# ── NoticeData conversion ──────────────────────────────────────────────


def to_notice_data(rec: HallidayWatkinsRecord) -> "NoticeData":
    """Convert an HWM record to a NoticeData for the standard downstream."""
    from notice_parser import NoticeData
    n = NoticeData()
    n.notice_type = "foreclosure"
    n.county = (rec.county or "").strip().title()
    n.state = "AL"
    n.address = rec.address
    n.city = rec.city
    n.zip = rec.zipcode
    n.auction_date = _mmddyyyy(rec.sale_date)
    n.date_added = date.today().strftime("%Y-%m-%d")
    n.received_date = n.date_added
    n.source_url = HWM_AL_URL
    n.case_number = rec.file_number
    n.trustee = "Halliday, Watkins & Mann, P.C."
    n.raw_text = (
        f"Foreclosure | Auction: {n.auction_date} | Municipality: {rec.city} | "
        f"Trustee: Halliday, Watkins & Mann, P.C. | File#: {rec.file_number} | "
        f"Sale Status: {rec.sale_status or 'scheduled'} | "
        f"Source: Halliday Watkins Mann AL portal"
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
    # Propagate the ZIP-estimation flag into the DataSift Data Flags column
    # so filter presets can exclude fallback-tiered records if precision
    # matters. Same convention as pre_probate_pipeline_al.
    if rec.zip_estimated_from_city:
        existing = n.missing_data_flags or ""
        n.missing_data_flags = (
            f"{existing}|zip_estimated_from_city" if existing
            else "zip_estimated_from_city"
        )
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
    enrich_owner: bool = True,
    include_cancelled: bool = False,
) -> list["NoticeData"]:
    """One-shot fetch → county filter → enrich → tier filter → NoticeData.

    Note the flow order differs from RL/T&B: enrichment MUST run before
    tier filter because HWM strips ZIP from most records. Enrichment is
    forced on by default (unlike RL/T&B where it's opt-in) — otherwise
    the tier filter would drop 90%+ of records.
    """
    records = fetch_hwm_al()
    logger.info("HWM AL: %d total records fetched", len(records))
    records = filter_by_county(
        records, counties, include_cancelled=include_cancelled,
    )
    logger.info(
        "HWM AL: %d records after county filter (counties=%s)",
        len(records), counties,
    )

    if enrich_owner:
        hits = 0
        for r in records:
            if enrich_with_owner(r):
                hits += 1
        logger.info("HWM AL: enrichment recovered ZIP+owner for %d/%d records",
                    hits, len(records))

        # Fallback: fill remaining empty owner_names from the APN owner
        # cache (see src/owner_cache.py). HWM's own enrichment recovers
        # both ZIP and owner from the tax roll; the cache only backfills
        # owner_name — records that ended up with a ZIP but no owner get
        # a second-chance name from APN's mortgagor extraction.
        try:
            import owner_cache
            filled = owner_cache.fill_missing_owners(records)
            if filled:
                logger.info("HWM AL: APN owner cache filled +%d additional "
                            "records", filled)
        except Exception as e:
            logger.debug("Owner cache fallback skipped: %s", e)

    if tiers is not None:
        records = filter_by_tier(records, tiers)
        logger.info(
            "HWM AL: %d records after tier filter (tiers=%s)",
            len(records), tiers,
        )

    return [to_notice_data(r) for r in records]


# ── CLI ────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="halliday_watkins_al_pipeline",
        description="Halliday Watkins Mann AL foreclosure schedule → NoticeData / CSV.",
    )
    p.add_argument(
        "--counties", default="Jefferson,Madison,Marshall",
        help="Comma-separated counties (default: Jefferson,Madison,Marshall).",
    )
    p.add_argument(
        "--tiers", default="1,2",
        help="Comma-separated ZIP tiers (default '1,2', 'all' disables). "
             "Applied AFTER owner enrichment recovers ZIP from tax roll.",
    )
    p.add_argument(
        "--enrich-owner", action="store_true", default=True,
        help="Enrich owner_name + ZIP via county property API (~0.3s/record). "
             "ON by default — required for tier filter since HWM strips "
             "ZIP from most records.",
    )
    p.add_argument(
        "--no-enrich-owner", dest="enrich_owner", action="store_false",
        help="Skip owner enrichment (tier filter will then drop ~90%%).",
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
        help="Keep cancelled/sold/withdrawn sales (default: dropped).",
    )
    p.add_argument(
        "--output-csv", type=Path, default=None,
        help="Write standard Sift-format CSV.",
    )
    p.add_argument(
        "--output-datasift-csv", type=Path, default=None,
        help="Write DataSift-format CSV (80 cols).",
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
        records = fetch_hwm_al()
        records = filter_by_county(
            records, counties, include_cancelled=args.include_cancelled,
        )
        if args.enrich_owner:
            for r in records:
                enrich_with_owner(r)
        if tiers is not None:
            records = filter_by_tier(records, tiers)
        print(json.dumps([asdict(r) for r in records], indent=2))
        return 0

    notices = fetch_all(
        counties=counties,
        tiers=tiers,
        enrich_owner=args.enrich_owner,
        include_cancelled=args.include_cancelled,
    )

    print(f"\n=== Halliday Watkins Mann AL — {len(notices)} notice(s) ===")
    for n in notices:
        owner = n.owner_name or "(no owner)"
        est = " [ZIP-est]" if "zip_estimated_from_city" in (
            n.missing_data_flags or ""
        ) else ""
        print(
            f"  {n.county:10s} {n.zip:5s}{est} {n.address:35s} sale={n.auction_date}  "
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
