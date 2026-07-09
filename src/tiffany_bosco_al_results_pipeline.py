"""Tiffany & Bosco AL — Sales Results (historical) adapter.

Companion to ``tiffany_bosco_al_pipeline.py`` (pending sales). Pulls the
past 180 days of completed T&B AL foreclosure sales from
``fs.tblaw.com/Sales/SalesResultsAl.aspx`` and emits ONLY records with
status ``Cancelled`` or ``Postponed``. Sold/3rd-Party/Reverted records
are dropped — the seller either already moved on (bank REO) or already
sold to a 3rd party, so there's no direct-outreach opportunity.

**Why this is a valuable data source:**
- ``Cancelled`` means the foreclosure was called off (mortgage cured,
  loan mod, negotiated resolution). The owner is under active distress
  but retained the property — high signal for wholesale outreach.
- ``Postponed`` means the trustee delayed the sale (buys the borrower
  time). Owner is still under distress but hasn't lost the property yet.

Both statuses indicate motivated sellers who aren't captured by the
pending sales adapter (which only shows currently-scheduled auctions).

**Deduplication (per operator 2026-07-09):**
1. Within a fetch: dedup by ``(file_number, sale_date, status)`` triple
   — T&B may list the same case multiple times if it was postponed then
   cancelled; keep every distinct (file, date, status) event.
2. Across runs: persistent ``.tb_results_seen_ids.json`` tracks
   previously-emitted triples so daily reruns don't re-upload records
   DataSift already has.
3. Cross-source with pending adapter: DataSift address-dedup handles
   this at upload time — no explicit filter needed here.

**Time window:** Fixed at 180 days back (per operator). T&B allows up
to 5 years but longer windows dilute lead quality (owner has moved on).

**Output:** Two separate CSVs written per run — one Cancelled, one
Postponed. Both notice_type=foreclosure so daily_finalize's
_categorize() routes them to the Foreclosure DataSift list, but each
carries ``notice_subtype = "foreclosure_cancelled"`` or
``foreclosure_postponed`` for DataSift filter-preset targeting.

Live 2026-07-09 default-window sample (last 7 days): 20 records,
statuses: Cancelled 13 / Reverted 4 / 3rd Party 3. 180-day sample
(pagination-driven): ~60-100 records typical.

CLI:
    python src/tiffany_bosco_al_results_pipeline.py
    python src/tiffany_bosco_al_results_pipeline.py --days-back 90
    python src/tiffany_bosco_al_results_pipeline.py \\
        --output-datasift-cancelled output/tb_cancelled.csv \\
        --output-datasift-postponed output/tb_postponed.csv \\
        --enrich-owner --skip-trace
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

TB_RESULTS_URL = "https://fs.tblaw.com/Sales/SalesResultsAl.aspx"
DEFAULT_DAYS_BACK = 180
MAX_DAYS_BACK = 180  # operator-imposed ceiling — no potential beyond this

# Persistent dedup — records already emitted across previous runs.
# Committed by the bot alongside seen_ids.json (opt-in via CLI flag so
# ad-hoc runs don't accidentally poison the file).
SEEN_IDS_PATH = Path(__file__).resolve().parent.parent / ".tb_results_seen_ids.json"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ListView row correlation on the ASP.NET page — same shape as pending
# sales, plus a status span at cell[6] of row A.
_SALE_DATE_ID_RE = re.compile(r"^ListView1_lblSaleDate_(\d+)$")

# Address format on the Results page (differs from Pending):
#   "114 Elledge Farm Dr, Hazel Green 35750"  — no state, ZIP appended to city
# NOTE: the pending page uses "street, city, ST ZIP" (4-part comma-separated).
_ADDR_RESULTS_RE = re.compile(
    r"^\s*(.*?),\s*(.+?)\s+(\d{5}(?:-\d{4})?)\s*$"
)

# Actionable statuses — everything else drops. Loose substring match so
# "Cancelled by borrower request" etc still classify correctly.
_CANCELLED_MARKERS = ("cancel",)
_POSTPONED_MARKERS = ("postpone",)


@dataclass
class TiffanyBoscoResultRecord:
    """One row from T&B's Sales Results ListView, with status."""

    sale_date_raw: str = ""     # "7/2/2026"
    sale_date: str = ""         # "2026-07-02" ISO
    file_number: str = ""       # "25-10801-PM-AL"
    loan_date: str = ""
    original_loan_amount: str = ""
    address: str = ""           # "114 Elledge Farm Dr"
    city: str = ""              # "Hazel Green"
    state: str = "AL"
    zipcode: str = ""           # "35750"
    county: str = ""            # "Madison"
    place_of_sale: str = ""
    status_raw: str = ""        # "Cancelled" / "Postponed" / "Reverted" / "3rd Party"
    outcome: str = ""           # normalized: "cancelled" / "postponed" / "" (drop)

    # Owner enrichment (same shape as pending adapter)
    owner_name: str = ""
    parcel_id: str = ""
    assessed_value: str = ""
    is_homestead: bool = False

    def dedup_key(self) -> str:
        """Cross-run dedup key: file# + sale_date + status."""
        return f"{self.file_number}|{self.sale_date}|{self.outcome}"


# ── Fetch — POST-back with pagination ──────────────────────────────────


def fetch_tb_results(
    *,
    days_back: int = DEFAULT_DAYS_BACK,
    timeout: float = 60.0,
    url: str = TB_RESULTS_URL,
    max_pages: int = 20,
) -> list[TiffanyBoscoResultRecord]:
    """Fetch T&B Sales Results across the requested window with pagination.

    T&B's Sales Results page defaults to the past 7 days (~20 records).
    To reach 180 days requires an ASP.NET POST-back with the search form
    widened, then iterating through pager links. Each POST returns 20
    records; typical 180-day span is 3-5 pages.

    ``max_pages`` is a safety guard — 20 pages × 20 records = 400
    records covers even a very active 180-day span. Set higher for
    special backfills.
    """
    if days_back > MAX_DAYS_BACK:
        logger.warning(
            "days_back=%d exceeds operator ceiling %d; clamping.",
            days_back, MAX_DAYS_BACK,
        )
        days_back = MAX_DAYS_BACK

    session = requests.Session()
    session.headers.update({"User-Agent": _UA})
    session.cookies.set("iagree", "true", domain="fs.tblaw.com")

    logger.info(
        "Fetching T&B AL Sales Results — %d day window ending %s",
        days_back, date.today().isoformat(),
    )
    # Step 1: GET landing page for viewstate tokens
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()

    today = date.today()
    start = date.fromordinal(today.toordinal() - days_back)

    # Step 2: initial search POST-back to widen the date range
    tokens = _extract_hidden_tokens(resp.text)
    form = _build_search_form(tokens, start, today)
    resp = session.post(url, data=form, timeout=timeout)
    resp.raise_for_status()

    records: list[TiffanyBoscoResultRecord] = []
    seen_pages = 0
    while seen_pages < max_pages:
        page_records = _parse_records(resp.text)
        records.extend(page_records)
        logger.debug(
            "T&B Results page %d: %d records (running total %d)",
            seen_pages + 1, len(page_records), len(records),
        )

        # Look for a "next" pager link
        next_target = _extract_next_pager_target(resp.text)
        if not next_target:
            break
        seen_pages += 1

        tokens = _extract_hidden_tokens(resp.text)
        form = _build_pager_form(tokens, next_target, start, today)
        resp = session.post(url, data=form, timeout=timeout)
        resp.raise_for_status()

    logger.info("T&B Results: fetched %d total records across %d pages",
                len(records), seen_pages + 1)
    return records


def _extract_hidden_tokens(html: str) -> dict[str, str]:
    """Pull VIEWSTATE + related ASP.NET tokens from a form's hidden inputs."""
    def _find(name: str) -> str:
        m = re.search(
            rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', html,
        )
        return m.group(1) if m else ""
    return {
        "__VIEWSTATE": _find("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _find("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _find("__EVENTVALIDATION"),
    }


def _build_search_form(
    tokens: dict[str, str], start: date, end: date,
) -> dict[str, str]:
    """Search-form POST payload — initial widened-window query."""
    return {
        "__EVENTTARGET": "btnSearch",
        "__EVENTARGUMENT": "",
        "__LASTFOCUS": "",
        "__VIEWSTATE": tokens["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": tokens["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": tokens["__EVENTVALIDATION"],
        "tbSaleStart": start.strftime("%-m/%-d/%Y"),
        "tbSaleEnd": end.strftime("%-m/%-d/%Y"),
        "tbAddrFilter": "",
        "tbCityFilter": "",
        "tbCountyFilter": "",
        "tbZipFilter": "",
        "tbFileFilter": "",
        "btnSearch": "Search",
    }


def _build_pager_form(
    tokens: dict[str, str], event_target: str, start: date, end: date,
) -> dict[str, str]:
    """Pager-click POST payload — replays date range + advances page."""
    return {
        "__EVENTTARGET": event_target,
        "__EVENTARGUMENT": "",
        "__LASTFOCUS": "",
        "__VIEWSTATE": tokens["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": tokens["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": tokens["__EVENTVALIDATION"],
        "tbSaleStart": start.strftime("%-m/%-d/%Y"),
        "tbSaleEnd": end.strftime("%-m/%-d/%Y"),
        "tbAddrFilter": "",
        "tbCityFilter": "",
        "tbCountyFilter": "",
        "tbZipFilter": "",
        "tbFileFilter": "",
    }


def _extract_next_pager_target(html: str) -> str:
    """Find the __doPostBack target for the "next page" link, if any.

    T&B renders the pager as `<span class="current">N</span>` for the
    active page, followed by anchor tags for pages N+1, N+2, ..., then
    "›" and "»". The lowest-numbered anchor after the current page is
    the "next" link. Returns "" when we're already on the last page.
    """
    soup = BeautifulSoup(html, "html.parser")
    pager = soup.find("span", id=re.compile(r"^ListView1_PagerTop$"))
    if not pager:
        return ""
    # The current-page marker
    current_marker = pager.find("span", class_="current")
    if current_marker is None:
        return ""
    try:
        current_page = int(current_marker.get_text(strip=True))
    except (ValueError, TypeError):
        return ""
    # Look for the anchor whose visible text is (current_page + 1) —
    # that's the next-page link. Fall back to the "›" (next) arrow when
    # numeric pages 2+ aren't in view (rare — usually only when there
    # are 10+ pages and the pager truncates).
    for a in pager.find_all("a"):
        txt = a.get_text(strip=True)
        if txt == str(current_page + 1):
            m = re.search(r"__doPostBack\('([^']+)'", a.get("href", ""))
            if m:
                return m.group(1)
    # Fallback: "›" arrow
    for a in pager.find_all("a"):
        if a.get_text(strip=True) == "›":  # ›
            m = re.search(r"__doPostBack\('([^']+)'", a.get("href", ""))
            if m:
                return m.group(1)
    return ""


# ── Parse ──────────────────────────────────────────────────────────────


def _parse_records(html: str) -> list[TiffanyBoscoResultRecord]:
    """Parse all records visible on a single page of the Sales Results."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[TiffanyBoscoResultRecord] = []
    for span in soup.find_all("span", id=_SALE_DATE_ID_RE):
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
) -> TiffanyBoscoResultRecord | None:
    rec = TiffanyBoscoResultRecord()

    # Row A: sale date, file#, loan date, orig loan, status (cell[6])
    rec.sale_date_raw = sale_date_span.get_text(strip=True)
    try:
        rec.sale_date = datetime.strptime(
            rec.sale_date_raw, "%m/%d/%Y",
        ).strftime("%Y-%m-%d")
    except ValueError:
        rec.sale_date = ""

    tr_a = sale_date_span.find_parent("tr")
    if tr_a:
        tds = tr_a.find_all("td")
        if len(tds) > 1:
            rec.file_number = tds[1].get_text(strip=True)
        if len(tds) > 2:
            rec.loan_date = tds[2].get_text(strip=True)
        if len(tds) > 4:
            rec.original_loan_amount = tds[4].get_text(strip=True)

    # Status — separate span (Row A cell[6]). May contain nested color span.
    status_span = soup.find("span", id=f"ListView1_lblFileStatus_{idx}")
    if status_span:
        rec.status_raw = status_span.get_text(strip=True)
    rec.outcome = _classify_outcome(rec.status_raw)

    # Row B (the itemRow immediately following Row A) contains the
    # address in cell[0] as plain text and the county in cell[1].
    # This differs from the Pending Sales page which uses a GMap
    # LinkButton for the address and a Place-of-Sale span for the row
    # containing county. On Results page, neither ListView1_LinkButton1
    # nor ListView1_lblSaleLoc exist per the 2026-07-09 markup probe.
    if tr_a:
        tr_b = tr_a.find_next_sibling("tr", class_="itemRow")
        if tr_b:
            tds_b = tr_b.find_all("td")
            if tds_b:
                # cell[0] carries the address string as plain text.
                # Format: "street, city ZIP" (space-separated ZIP, no state).
                raw = " ".join(tds_b[0].get_text().split())
                parts = _ADDR_RESULTS_RE.match(raw)
                if parts:
                    rec.address = parts.group(1).strip()
                    rec.city = parts.group(2).strip()
                    rec.zipcode = parts.group(3).strip()[:5]
                else:
                    # Fallback: no recognizable ZIP — keep raw address.
                    rec.address = raw
            if len(tds_b) >= 2:
                rec.county = tds_b[1].get_text(strip=True)

    return rec


def _classify_outcome(status: str) -> str:
    """Loose classification: 'cancelled' / 'postponed' / '' (drop)."""
    s = (status or "").lower()
    if any(m in s for m in _CANCELLED_MARKERS):
        return "cancelled"
    if any(m in s for m in _POSTPONED_MARKERS):
        return "postponed"
    return ""  # Sold / Reverted / 3rd Party / etc — drop


# ── Filter + dedup ─────────────────────────────────────────────────────


def filter_and_dedup(
    records: list[TiffanyBoscoResultRecord],
    *,
    counties: tuple[str, ...],
    tiers: tuple[int, ...] | None,
    seen_ids: set[str] | None = None,
) -> list[TiffanyBoscoResultRecord]:
    """County + tier filter + drop non-{cancelled,postponed} + dedup.

    Dedup rules:
      1. Drop records where outcome is neither cancelled nor postponed.
      2. Within-fetch: dedup by ``(file#, sale_date, outcome)`` — keeps
         every meaningful action but drops literal duplicates from T&B's
         listing.
      3. Cross-run: drop any record whose ``dedup_key()`` is in
         ``seen_ids`` (loaded from ``.tb_results_seen_ids.json``).
    """
    # 1) drop non-actionable statuses
    kept = [r for r in records if r.outcome]

    # 2) county filter
    wanted = {c.lower() for c in counties}
    kept = [r for r in kept if (r.county or "").lower() in wanted]

    # 3) tier filter (records without ZIP drop; owner enrichment can
    # recover ZIP but for a bulk fetch we defer that to a later stage)
    if tiers is not None:
        from target_zips import zip_tier_county
        tier_set = set(tiers)
        with_tier: list[TiffanyBoscoResultRecord] = []
        for r in kept:
            zip5 = (r.zipcode or "").strip()[:5]
            if not zip5:
                # Keep records without ZIP for owner-enrichment recovery
                # — enrichment fills ZIP from the tax roll, then we
                # re-check tier at NoticeData conversion time.
                with_tier.append(r)
                continue
            t, _ = zip_tier_county(zip5)
            if t in tier_set:
                with_tier.append(r)
        kept = with_tier

    # 4) within-fetch dedup
    seen_local: set[str] = set()
    unique: list[TiffanyBoscoResultRecord] = []
    for r in kept:
        k = r.dedup_key()
        if k in seen_local:
            continue
        seen_local.add(k)
        unique.append(r)

    # 5) cross-run dedup
    if seen_ids:
        unique = [r for r in unique if r.dedup_key() not in seen_ids]

    return unique


# ── Persistent seen_ids ────────────────────────────────────────────────


def load_seen_ids(path: Path = SEEN_IDS_PATH) -> set[str]:
    """Load the cross-run dedup set. Missing file → empty set."""
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception as e:
        logger.warning("Failed to load %s: %s — treating as empty", path, e)
        return set()


def save_seen_ids(seen: set[str], path: Path = SEEN_IDS_PATH) -> None:
    """Persist the updated dedup set."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, indent=1)
    except Exception as e:
        logger.warning("Failed to save %s: %s", path, e)


# ── Owner enrichment (reuse pending adapter's helpers) ─────────────────


def enrich_with_owner(rec: TiffanyBoscoResultRecord) -> bool:
    """Fill owner_name + parcel_id via county property API."""
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
        logger.debug("Owner enrichment failed for %r (%s): %s",
                     rec.address, county, e)
    if not matches:
        return False
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
        getattr(m, "is_homestead", False)
        or getattr(m, "is_buildable", False)
    )
    # Recover ZIP from tax roll when T&B stripped it
    situs_zip = getattr(m, "situs_zip", "") or ""
    if situs_zip and not rec.zipcode:
        rec.zipcode = situs_zip[:5]
    return bool(rec.owner_name)


def _split_house_num(address: str) -> tuple[str, str]:
    m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", address or "")
    if not m:
        return ("", address or "")
    return (m.group(1), m.group(2))


# ── NoticeData conversion ──────────────────────────────────────────────


def to_notice_data(rec: TiffanyBoscoResultRecord) -> "NoticeData":
    """Convert a T&B Results record to a NoticeData.

    ``notice_subtype`` distinguishes cancelled vs postponed so DataSift
    filter presets can route each to its own outreach sequence.
    """
    from notice_parser import NoticeData
    n = NoticeData()
    n.notice_type = "foreclosure"
    n.notice_subtype = f"foreclosure_{rec.outcome}"  # foreclosure_cancelled | foreclosure_postponed
    n.county = (rec.county or "").strip().title()
    n.state = "AL"
    n.address = rec.address
    n.city = rec.city
    n.zip = rec.zipcode
    n.auction_date = _mmddyyyy(rec.sale_date)
    n.date_added = date.today().strftime("%Y-%m-%d")
    n.received_date = n.date_added
    n.source_url = TB_RESULTS_URL
    n.case_number = rec.file_number
    n.trustee = "Tiffany & Bosco, P.A."
    n.raw_text = (
        f"Foreclosure (historical, status={rec.status_raw}) | "
        f"Auction: {n.auction_date} | Municipality: {rec.city} | "
        f"Trustee: Tiffany & Bosco, P.A. | File#: {rec.file_number} | "
        f"Original Loan: {rec.original_loan_amount} | "
        f"Sale Location: {rec.place_of_sale} | "
        f"Source: Tiffany & Bosco AL Sales Results portal"
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
    days_back: int = DEFAULT_DAYS_BACK,
    enrich_owner: bool = False,
    use_seen_ids: bool = True,
    seen_ids_path: Path = SEEN_IDS_PATH,
) -> tuple[list["NoticeData"], list["NoticeData"], set[str]]:
    """One-shot fetch + filter + dedup + enrich + split.

    Returns ``(cancelled_notices, postponed_notices, updated_seen_ids)``.
    The seen_ids set is returned so the caller can decide whether to
    persist it (typically only after successful CSV writes).
    """
    records = fetch_tb_results(days_back=days_back)
    logger.info("T&B Results: %d raw records fetched", len(records))

    seen_ids = load_seen_ids(seen_ids_path) if use_seen_ids else set()
    filtered = filter_and_dedup(
        records, counties=counties, tiers=tiers,
        seen_ids=seen_ids,
    )
    logger.info(
        "T&B Results: %d records after filter+dedup (seen_ids had %d)",
        len(filtered), len(seen_ids),
    )

    if enrich_owner:
        hits = 0
        for r in filtered:
            if enrich_with_owner(r):
                hits += 1
        logger.info("T&B Results: owner enrichment filled %d/%d",
                    hits, len(filtered))

    # After enrichment, re-apply tier filter for records where ZIP was
    # recovered from the tax roll (was empty during initial filter).
    if tiers is not None:
        from target_zips import zip_tier_county
        tier_set = set(tiers)
        re_gated: list[TiffanyBoscoResultRecord] = []
        for r in filtered:
            zip5 = (r.zipcode or "").strip()[:5]
            if not zip5:
                continue  # still no ZIP after enrichment — drop
            t, _ = zip_tier_county(zip5)
            if t in tier_set:
                re_gated.append(r)
        dropped = len(filtered) - len(re_gated)
        if dropped:
            logger.info(
                "T&B Results: post-enrichment tier re-gate dropped %d records "
                "(no ZIP recovered or off-tier)", dropped,
            )
        filtered = re_gated

    # Split by outcome
    cancelled = [
        to_notice_data(r) for r in filtered if r.outcome == "cancelled"
    ]
    postponed = [
        to_notice_data(r) for r in filtered if r.outcome == "postponed"
    ]
    logger.info(
        "T&B Results: emitting %d cancelled + %d postponed",
        len(cancelled), len(postponed),
    )

    # Update seen_ids for the next run
    updated_seen = set(seen_ids)
    for r in filtered:
        updated_seen.add(r.dedup_key())

    return cancelled, postponed, updated_seen


# ── CLI ────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tiffany_bosco_al_results_pipeline",
        description="Tiffany & Bosco AL Sales Results (Cancelled + Postponed only) → NoticeData / CSV.",
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
        "--days-back", type=int, default=DEFAULT_DAYS_BACK,
        help=f"Days of history to pull (max {MAX_DAYS_BACK}).",
    )
    p.add_argument(
        "--enrich-owner", action="store_true",
        help="Enrich owner_name + parcel_id + ZIP via county property API.",
    )
    p.add_argument(
        "--skip-trace", action="store_true",
        help="Tracerfy skip-trace + Trestle scoring on records with "
             "owner_name (fills Phone / Email columns).",
    )
    p.add_argument(
        "--no-seen-ids", dest="use_seen_ids", action="store_false",
        help="Skip cross-run dedup (emit all matching records regardless "
             "of prior runs). Useful for ad-hoc diagnostic runs.",
    )
    p.add_argument(
        "--output-datasift-cancelled", type=Path, default=None,
        help="Write DataSift CSV for CANCELLED records.",
    )
    p.add_argument(
        "--output-datasift-postponed", type=Path, default=None,
        help="Write DataSift CSV for POSTPONED records.",
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
        records = fetch_tb_results(days_back=args.days_back)
        seen = load_seen_ids() if args.use_seen_ids else set()
        filtered = filter_and_dedup(
            records, counties=counties, tiers=tiers, seen_ids=seen,
        )
        if args.enrich_owner:
            for r in filtered:
                enrich_with_owner(r)
        print(json.dumps([asdict(r) for r in filtered], indent=2))
        return 0

    cancelled, postponed, updated_seen = fetch_all(
        counties=counties,
        tiers=tiers,
        days_back=args.days_back,
        enrich_owner=args.enrich_owner,
        use_seen_ids=args.use_seen_ids,
    )

    print(f"\n=== T&B Results — {len(cancelled)} cancelled + {len(postponed)} postponed ===")
    for n in cancelled + postponed:
        sub = n.notice_subtype.replace("foreclosure_", "").upper()
        owner = n.owner_name or "(no owner)"
        print(
            f"  [{sub[:8]:8s}] {n.county:10s} {n.zip:5s} {n.address:35s} "
            f"sale={n.auction_date}  file={n.case_number}  owner={owner}"
        )

    # Tracerfy skip-trace + Trestle — same shape as pending adapter
    phone_tiers_cancelled: dict | None = None
    phone_tiers_postponed: dict | None = None
    if args.skip_trace:
        for label, notices, tiers_ref in [
            ("cancelled", cancelled, "phone_tiers_cancelled"),
            ("postponed", postponed, "phone_tiers_postponed"),
        ]:
            if not notices:
                continue
            traceable = [n for n in notices if (n.owner_name or "").strip()]
            if not traceable:
                continue
            try:
                import tracerfy_skip_tracer
                stats = tracerfy_skip_tracer.batch_skip_trace(traceable)
                logger.info(
                    "%s skip-trace: submitted=%d matched=%d phones=%d "
                    "emails=%d cost=$%.2f",
                    label, stats.get("submitted", 0), stats.get("matched", 0),
                    stats.get("phones_found", 0),
                    stats.get("emails_found", 0),
                    stats.get("cost", 0.0),
                )
            except Exception as e:
                logger.warning("%s skip-trace failed: %s", label, e)
            try:
                from phone_validator import score_phones_for_pipeline
                tiers_val = score_phones_for_pipeline(notices)
                if label == "cancelled":
                    phone_tiers_cancelled = tiers_val
                else:
                    phone_tiers_postponed = tiers_val
            except Exception as e:
                logger.warning("%s Trestle scoring failed: %s", label, e)

    from datasift_formatter import write_datasift_csv
    if args.output_datasift_cancelled and cancelled:
        path = write_datasift_csv(
            cancelled, str(args.output_datasift_cancelled),
            phone_tiers=phone_tiers_cancelled,
        )
        print(f"Wrote Cancelled DataSift CSV: {path}")
    elif args.output_datasift_cancelled:
        print("(No cancelled records — skipping cancelled CSV write.)")
    if args.output_datasift_postponed and postponed:
        path = write_datasift_csv(
            postponed, str(args.output_datasift_postponed),
            phone_tiers=phone_tiers_postponed,
        )
        print(f"Wrote Postponed DataSift CSV: {path}")
    elif args.output_datasift_postponed:
        print("(No postponed records — skipping postponed CSV write.)")

    # Persist seen_ids only if at least one CSV was written (avoids
    # skipping records forever if the CSV write failed for an unrelated
    # reason — e.g., disk full).
    if (args.output_datasift_cancelled and cancelled) or (
        args.output_datasift_postponed and postponed
    ):
        if args.use_seen_ids:
            save_seen_ids(updated_seen)
            logger.info("Updated seen_ids saved to %s (%d entries)",
                        SEEN_IDS_PATH, len(updated_seen))

    return 0 if (cancelled or postponed) else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
