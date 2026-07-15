"""Rubin Lublin, LLC — Alabama foreclosure sale schedule adapter.

Rubin Lublin publishes their entire statewide AL non-judicial foreclosure
sale schedule as a public HTML table at:

    https://rlselaw.com/property-listing/alabama-property-listings/

The page is server-rendered HTML with a clean 7-column layout: Sale Date,
File #, Property (street), City, Zip, County, Bid link. No auth, no
CAPTCHA, no JS gating. Update cadence appears daily-ish (attorneys post
new sales as files come in).

This adapter closes the Madison + Marshall County foreclosure coverage
gap: alabamapublicnotices.com (APN) returns 0 Madison/Marshall foreclosures
because The Madison County Record (the primary Madison publisher) is NOT
an APN participating publication — verified 2026-07-08. Rubin Lublin's
public portal + Tiffany & Bosco's public portal (fs.tblaw.com) are our
two primary paths to Madison + Marshall foreclosures until an APN
alternative surfaces.

Live 2026-07-08 snapshot: 117 total AL records / 8 Madison / 3 Marshall
(all 3 Marshall in Tier 1; 5 of 8 Madison in Tier 1+2).

Per-record fields the portal exposes:
    - Sale date + auction time window ("07/16/2026 (11am - 4pm)")
    - RL File # (e.g. "26-01908")
    - Property street address
    - City + ZIP
    - County
    - Bid submission portal link (Auction.com, Xome, hubzu, Servicelink)

Owner name is NOT on the portal listing (available only on the underlying
Notice of Sale PDF). We enrich via county property API address-search
(existing Jefferson E-Ring / Madison AssuranceWeb / Marshall AssuranceWeb
adapters) — same pattern as birmingham_code_enforcement_api.enrich_with_owner.

CLI:
    python src/rubin_lublin_al_pipeline.py
    python src/rubin_lublin_al_pipeline.py --counties Madison,Marshall
    python src/rubin_lublin_al_pipeline.py --tiers 1,2 --enrich-owner \\
        --output-datasift-csv output/rl_foreclosures.csv
"""
from __future__ import annotations

import argparse
import json
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

RUBIN_LUBLIN_AL_URL = (
    "https://rlselaw.com/property-listing/alabama-property-listings/"
)

# Cross-run dedup — matches T&B Results pattern (c427ad3). The RL portal
# lists foreclosures for the full statutory publication window (~3 weeks
# before auction), so without persistent dedup we re-emit every property
# ~15-21 times over its listing lifetime. DataSift's address-dedup at
# upload prevents duplicate ROWS but adds a fresh "comment" to the lead
# each day — the user sees weeks of redundant "Foreclosure | Auction: X"
# notes on the same lead, obscuring which record is actually new today.
# Key: "file_number|sale_date". Sale-date change (postponement) → new key
# → correctly re-emitted with the updated date.
_SEEN_IDS_PATH = Path(__file__).resolve().parent.parent / ".rl_seen_ids.json"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Match the leading MM/DD/YYYY portion of the sale-date cell so we can
# parse out just the date (auction time window is retained separately).
_DATE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})")
# Auction time window like "(11am - 4pm)"
_TIME_WINDOW_RE = re.compile(r"\(([^)]+)\)")


@dataclass
class RubinLublinRecord:
    """One row of Rubin Lublin's Alabama property listings table."""

    sale_date_raw: str = ""     # "07/16/2026 (11am - 4pm)"
    sale_date: str = ""         # "2026-07-16" (ISO)
    auction_time_window: str = ""  # "11am - 4pm"
    file_number: str = ""       # "26-01908"
    address: str = ""           # "107 Elledge Farm Dr"
    city: str = ""              # "Hazel Green"
    zipcode: str = ""           # "35750"
    county: str = ""            # "Madison"
    bid_link: str = ""          # "Go to Auction.com" or "Not Available"

    # Owner enrichment (populated by enrich_with_owner when --enrich-owner)
    owner_name: str = ""
    parcel_id: str = ""
    assessed_value: str = ""
    is_homestead: bool = False


# ── Fetch + parse ──────────────────────────────────────────────────────


def fetch_rubin_lublin_al(
    *,
    timeout: float = 30.0,
    url: str = RUBIN_LUBLIN_AL_URL,
) -> list[RubinLublinRecord]:
    """Fetch and parse Rubin Lublin's Alabama property listings page.

    Returns every row in the table (all AL counties). Filter downstream
    via `filter_records()` for county / tier selection.
    """
    logger.info("Fetching Rubin Lublin AL listings: %s", url)
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    return _parse_html(resp.text)


def _parse_html(html: str) -> list[RubinLublinRecord]:
    soup = BeautifulSoup(html, "html.parser")

    # The main listings table has one <table> matching the 7-col header
    # (Sale Date | File # | Property | City | Zip | County | Bid). Find
    # by presence of a <th data-sort> — RL's page has one such table.
    table = None
    for t in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in t.find_all("th")]
        if "Sale Date" in headers and "County" in headers:
            table = t
            break
    if table is None:
        logger.warning("Rubin Lublin table not found in fetched HTML")
        return []

    records: list[RubinLublinRecord] = []
    for tr in table.find_all("tr"):
        cells: dict[str, str] = {}
        for td in tr.find_all("td"):
            cls = td.get("class") or []
            if cls:
                cells[cls[0]] = td.get_text(strip=True)
        if not cells.get("property"):
            continue  # Header row or empty row

        rec = RubinLublinRecord()
        rec.sale_date_raw = cells.get("date", "")
        rec.sale_date = _parse_sale_date_iso(rec.sale_date_raw)
        rec.auction_time_window = _parse_time_window(rec.sale_date_raw)
        rec.file_number = cells.get("case", "")
        rec.address = cells.get("property", "")
        rec.city = cells.get("city", "")
        rec.zipcode = cells.get("zip", "")
        rec.county = cells.get("county", "")
        rec.bid_link = cells.get("bid", "")
        records.append(rec)
    return records


def _parse_sale_date_iso(raw: str) -> str:
    """'07/16/2026 (11am - 4pm)' → '2026-07-16'."""
    m = _DATE_RE.match(raw or "")
    if not m:
        return ""
    try:
        return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_time_window(raw: str) -> str:
    m = _TIME_WINDOW_RE.search(raw or "")
    return m.group(1).strip() if m else ""


# ── Filters ────────────────────────────────────────────────────────────


def filter_records(
    records: list[RubinLublinRecord],
    *,
    counties: tuple[str, ...] | None = None,
    tiers: tuple[int, ...] | None = None,
) -> list[RubinLublinRecord]:
    """Filter records by county and/or ZIP tier.

    ``counties`` — set to ("Jefferson", "Madison", "Marshall") for the
    default SiftStack scope. Case-insensitive match against ``record.county``.

    ``tiers`` — set to (1, 2) to keep only Tier-1 + Tier-2 ZIPs per
    ``target_zips.zip_tier_county``. Records without a ZIP or in non-tier
    ZIPs are dropped.
    """
    result = list(records)

    if counties:
        wanted = {c.lower() for c in counties}
        result = [r for r in result if (r.county or "").lower() in wanted]

    if tiers is not None:
        from target_zips import zip_tier_county
        tier_set = set(tiers)
        kept: list[RubinLublinRecord] = []
        for r in result:
            zip5 = (r.zipcode or "").strip()[:5]
            if not zip5:
                continue
            t, _ = zip_tier_county(zip5)
            if t in tier_set:
                kept.append(r)
        result = kept

    return result


# ── Owner enrichment (via existing property APIs) ──────────────────────


def enrich_with_owner(rec: RubinLublinRecord) -> bool:
    """Fill owner_name/parcel_id/assessed_value via county property API.

    Routes to Jefferson E-Ring, Madison AssuranceWeb, or Marshall
    AssuranceWeb depending on ``rec.county``. Returns True if the lookup
    filled owner_name; False otherwise. Cheap (~0.3s per lookup).
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
            # Madison adapter expects (street_number, street_name)
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
    rec.is_homestead = bool(getattr(m, "is_homestead", False)
                            or getattr(m, "is_buildable", False))
    return bool(rec.owner_name)


def _split_house_num(address: str) -> tuple[str, str]:
    """'107 Elledge Farm Dr' → ('107', 'Elledge Farm Dr')."""
    m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", address or "")
    if not m:
        return ("", address or "")
    return (m.group(1), m.group(2))


# ── NoticeData conversion ──────────────────────────────────────────────


def to_notice_data(rec: RubinLublinRecord) -> "NoticeData":
    """Convert a Rubin Lublin record to a NoticeData ready for the
    standard SiftStack downstream (Tracerfy skip-trace, Trestle scoring,
    DataSift CSV write, seen-ids dedup).
    """
    from notice_parser import NoticeData
    n = NoticeData()
    n.notice_type = "foreclosure"
    n.county = (rec.county or "").strip().title()  # "Madison" / "Marshall" / "Jefferson"
    n.state = "AL"
    n.address = rec.address
    n.city = rec.city
    n.zip = rec.zipcode
    n.auction_date = _mmddyyyy(rec.sale_date)
    n.date_added = date.today().strftime("%Y-%m-%d")
    n.received_date = n.date_added
    n.source_url = RUBIN_LUBLIN_AL_URL
    # File # goes into the case_number slot for cross-source correlation
    # with T&B's file# convention.
    n.case_number = rec.file_number
    n.trustee = "Rubin Lublin, LLC"
    # Notes summary — mirrors the format used by other foreclosure adapters
    # so daily_finalize.py's Notes regex parses it consistently.
    n.raw_text = (
        f"Foreclosure | Auction: {n.auction_date} | Municipality: {rec.city} | "
        f"Trustee: Rubin Lublin, LLC | File#: {rec.file_number} | "
        f"Bid: {rec.bid_link} | Source: Rubin Lublin AL portal"
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
    """'2026-07-16' → '7/16/2026' for foreclosure auction_date convention."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%-m/%-d/%Y")
    except ValueError:
        return iso


# ── Orchestrator ───────────────────────────────────────────────────────


def _dedup_key(rec: RubinLublinRecord) -> str:
    """(file_number, sale_date) key for cross-run dedup. Empty file# → empty
    key → record always emits (safer than a fuzzy address key)."""
    if not rec.file_number:
        return ""
    return f"{(rec.file_number or '').strip()}|{(rec.sale_date or '').strip()}"


def load_seen_ids(path: Path = _SEEN_IDS_PATH) -> set[str]:
    """Load the cross-run dedup set. Missing/corrupt file → empty set."""
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load %s (%s); starting fresh", path.name, e)
        return set()


def save_seen_ids(seen: set[str], path: Path = _SEEN_IDS_PATH) -> None:
    """Persist the updated dedup set."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, indent=1)
    except OSError as e:
        logger.warning("Failed to save %s: %s", path.name, e)


def fetch_all(
    *,
    counties: tuple[str, ...] = ("Jefferson", "Madison", "Marshall"),
    tiers: tuple[int, ...] | None = (1, 2),
    enrich_owner: bool = False,
    seen_ids: set[str] | None = None,
) -> tuple[list["NoticeData"], list[RubinLublinRecord]]:
    """One-shot fetch + filter + dedup + enrich + convert to NoticeData.

    Returns (notices, kept_records) so the caller can update seen_ids
    with dedup keys derived from the record's file_number + sale_date.
    """
    records = fetch_rubin_lublin_al()
    logger.info("Rubin Lublin AL: %d total records fetched", len(records))
    records = filter_records(records, counties=counties, tiers=tiers)
    logger.info(
        "Rubin Lublin AL: %d records after county/tier filter "
        "(counties=%s, tiers=%s)",
        len(records), counties, tiers,
    )

    if seen_ids is not None:
        before = len(records)
        records = [r for r in records if _dedup_key(r) not in seen_ids]
        skipped = before - len(records)
        if skipped:
            logger.info(
                "Rubin Lublin AL: cross-run dedup dropped %d already-seen "
                "records (kept %d)", skipped, len(records),
            )

    if enrich_owner:
        hits = 0
        for r in records:
            if enrich_with_owner(r):
                hits += 1
        logger.info("Rubin Lublin AL: owner enrichment filled %d/%d records",
                    hits, len(records))

        # Fallback: fill remaining empty owner_names from the APN owner cache
        # (foreclosure_owner_cache.json — populated by scripts/refresh_owner_cache.py
        # after main.py daily writes its APN foreclosure CSV). APN extracts the
        # mortgagor from the notice body itself; this catches properties whose
        # tax-roll owner name diverges from (or is missing on) the county API.
        try:
            import owner_cache
            filled = owner_cache.fill_missing_owners(records)
            if filled:
                logger.info("Rubin Lublin AL: APN owner cache filled +%d "
                            "additional records (%d/%d total)",
                            filled, hits + filled, len(records))
        except Exception as e:
            logger.debug("Owner cache fallback skipped: %s", e)

    return [to_notice_data(r) for r in records], records


# ── CLI ────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rubin_lublin_al_pipeline",
        description="Rubin Lublin AL foreclosure schedule → NoticeData / CSV.",
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
             "DataSift dial-priority tiers. Off by default to keep bulk "
             "pulls free; enable for daily-ops runs. ~$0.02/contact.",
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
    p.add_argument(
        "--no-seen-dedup", action="store_true",
        help="Ignore .rl_seen_ids.json cross-run dedup — re-emit every "
             "record currently on the portal (diagnostics / backfills).",
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
        records = fetch_rubin_lublin_al()
        records = filter_records(records, counties=counties, tiers=tiers)
        if args.enrich_owner:
            for r in records:
                enrich_with_owner(r)
        print(json.dumps([asdict(r) for r in records], indent=2))
        return 0

    seen_ids = set() if args.no_seen_dedup else load_seen_ids()
    logger.info("Loaded %d seen dedup keys from %s",
                len(seen_ids), _SEEN_IDS_PATH.name)

    notices, kept_records = fetch_all(
        counties=counties,
        tiers=tiers,
        enrich_owner=args.enrich_owner,
        seen_ids=seen_ids if not args.no_seen_dedup else None,
    )

    print(f"\n=== Rubin Lublin AL — {len(notices)} notice(s) ===")
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
    # code_violation_pipeline (f100e83) and apn_probate. Without this,
    # the DataSift Phone 1-9 / Email 1-5 columns stay empty and the
    # dial-priority filter presets can't route RL leads by tier.
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
            # Trestle scoring runs regardless of skip-trace outcome so any
            # existing phones on the records still tier correctly.
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

    # Persist seen-ids AFTER successful CSV write (defer until we've
    # actually delivered the records, so a mid-pipeline crash doesn't
    # lose them for tomorrow).
    if not args.no_seen_dedup and kept_records:
        for r in kept_records:
            key = _dedup_key(r)
            if key:
                seen_ids.add(key)
        save_seen_ids(seen_ids)
        logger.info("Persisted %d dedup keys → %s",
                    len(seen_ids), _SEEN_IDS_PATH.name)

    # "0 new records after dedup" is a healthy no-op, not a failure. Only
    # return non-zero on genuine unrecoverable errors (portal down, config
    # missing). Prior behavior returned 1 on empty output which surfaced
    # as red-X annotations on quiet dedup days.
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
