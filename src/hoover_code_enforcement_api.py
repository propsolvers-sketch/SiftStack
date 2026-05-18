"""Hoover, AL code-enforcement adapter via SeeClickFix public API.

Hoover (Jefferson County, Tier 1 ZIP 35022 + Tier 2 ZIP 35244 + adjacent
35226 / 35216) routes citizen-reported code violations through SeeClickFix's
311-style platform. The web portal at
``https://seeclickfix.com/web_portal/cfK8xFcB5G2XrMX9VzD1cLSc`` is Hoover's,
but its `web_portal_id` URL parameter doesn't actually filter the API.
Instead, we filter by lat/lng + zoom (bounds the bbox to Birmingham metro)
and post-filter strictly on "Hoover" in the address string.

Public API: ``https://seeclickfix.com/api/v2/issues`` — no auth, browser-like
User-Agent + Referer headers required to bypass anti-bot. ~1900 issues
within the bbox; ~360 strictly Hoover at any given time. The SeeClickFix
portal turns over fast — fresh issues land within hours, and most of the
data is from the last 30-60 days.

Code-enforcement signal:
- `request_type.title` starting with "CODE ENFORCEMENT" is the dedicated
  category — ~2-4 new Hoover issues per day (~700/year).
- This is a CITIZEN-COMPLAINT signal, not formal code citations. Indicates
  early-stage distress (overgrown grass, junk vehicles, dilapidated
  property) — softer than the Huntsville Unsafe Buildings list (formal
  condemnation) but earlier in the funnel.

Public API:

    fetch_code_violations(days_back=30, target_zips=ALL_TARGET) -> list[HooverSeeClickFixRecord]
    to_notice_data(rec, enrich_owner=True) -> NoticeData
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────


SCF_BASE = "https://seeclickfix.com/api/v2/issues"
SCF_PORTAL_URL = "https://seeclickfix.com/web_portal/cfK8xFcB5G2XrMX9VzD1cLSc"

# Hoover, AL center. zoom=11 covers ~10mi radius — captures all of Hoover
# proper (50 sq mi) plus spillover into Vestavia/Pelham which we post-filter.
HOOVER_CENTER_LAT = 33.4054
HOOVER_CENTER_LNG = -86.8114
HOOVER_ZOOM = 11

# Default request_type prefix — dedicated code-enforcement category.
# Other related types ("Report a Problem...", "Limbs/Debris...") tend to be
# trash-pickup / general-quality-of-life, not actionable distress signals.
DEFAULT_RT_PREFIXES = (
    "code enforcement",
)

# Browser-like headers — SeeClickFix's CDN rejects bare API user agents.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://seeclickfix.com/",
    "Origin": "https://seeclickfix.com",
}


# Address parsing — SeeClickFix returns address as a free-text string that
# varies in format across users:
#   "1905 River Woods Rd Hoover, Alabama, 35244"
#   "4560 Jessup Ln Hoover, AL, 35226, USA"
#   "1869 Patton Chapel Rd Hoover AL 35226, United States"
# Strategy: anchor on "Hoover" (we've already filtered), capture the
# ZIP at the tail, and treat everything before "Hoover" as the street.
_HOOVER_ANCHOR = re.compile(r"\bHoover\b", re.IGNORECASE)
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


# ── Record schema ────────────────────────────────────────────────────


@dataclass
class HooverSeeClickFixRecord:
    """One SeeClickFix code-enforcement issue from Hoover, AL."""

    issue_id: int
    status: str            # "Open" | "Acknowledged" | "Closed"
    request_type: str      # full title from request_type.title
    summary: str
    description: str       # often duplicates summary on Hoover

    # Property identity (parsed from the address string)
    address: str           # street only — "1905 River Woods Rd"
    city: str              # "Hoover"
    state: str             # "AL"
    zip: str               # 5-digit ZIP
    address_full: str      # raw address as returned by SeeClickFix

    # Geocode
    lat: float = 0.0
    lng: float = 0.0

    # Timestamps
    created_at: str = ""   # ISO 8601 with TZ
    updated_at: str = ""
    age_days: int = 0      # Days since created (computed at fetch time)

    # SeeClickFix-side links
    html_url: str = ""     # User-facing issue page
    api_url: str = ""      # JSON endpoint for this issue

    # Comments & engagement (rough quality signal)
    comment_count: int = 0
    rating: int = 1        # SeeClickFix internal "rating" — usually 1

    def to_dict(self) -> dict:
        return asdict(self)


# ── Fetch + parse ────────────────────────────────────────────────────


def _new_session(timeout: float = 20.0) -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    s.request = _bind_timeout(s.request, timeout)  # type: ignore
    return s


def _bind_timeout(fn, timeout):
    def _wrapped(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return fn(method, url, **kwargs)
    return _wrapped


def _parse_address(raw: str) -> tuple[str, str, str]:
    """Anchor-based parser for SeeClickFix address strings.

    SeeClickFix returns formats like:
      "1905 River Woods Rd Hoover, Alabama, 35244"
      "4560 Jessup Ln Hoover, AL, 35226, USA"
      "1869 Patton Chapel Rd Hoover AL 35226, United States"

    Strategy: locate the literal "Hoover" anchor — everything before it is
    the street, the trailing 5-digit token is the ZIP. Returns (street, city, zip).
    All three empty when the address can't be parsed.
    """
    if not raw:
        return ("", "", "")
    raw = raw.strip()

    anchor = _HOOVER_ANCHOR.search(raw)
    if not anchor:
        return ("", "", "")

    street = raw[: anchor.start()].strip().rstrip(",").strip()

    zip_match = _ZIP_RE.search(raw[anchor.end():])
    zip_code = zip_match.group(1) if zip_match else ""

    if not street or not zip_code:
        return ("", "", "")
    return (street, "Hoover", zip_code)


def _to_record(issue: dict) -> Optional[HooverSeeClickFixRecord]:
    """Map a SeeClickFix issue JSON dict → HooverSeeClickFixRecord.

    Returns None if address parsing fails or the issue doesn't look Hoover.
    """
    addr_raw = (issue.get("address") or "").strip()
    if "hoover" not in addr_raw.lower():
        return None  # spillover — Vestavia/Pelham/etc.

    street, city, zip_code = _parse_address(addr_raw)
    if not street or not zip_code:
        return None  # unparseable

    rt = issue.get("request_type") or {}
    rt_title = rt.get("title") if isinstance(rt, dict) else ""

    # Compute age in days
    age_days = 0
    created_at = issue.get("created_at") or ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
        except ValueError:
            pass

    return HooverSeeClickFixRecord(
        issue_id=int(issue.get("id") or 0),
        status=issue.get("status") or "",
        request_type=rt_title or "",
        summary=issue.get("summary") or "",
        description=(issue.get("description") or "").strip()[:500],
        address=street,
        city=city or "Hoover",
        state="AL",
        zip=zip_code,
        address_full=addr_raw,
        lat=float(issue.get("lat") or 0),
        lng=float(issue.get("lng") or 0),
        created_at=created_at,
        updated_at=issue.get("updated_at") or "",
        age_days=age_days,
        html_url=issue.get("html_url") or issue.get("url") or "",
        api_url=issue.get("url") or "",
        comment_count=int(issue.get("comment_count") or 0),
        rating=int(issue.get("rating") or 1),
    )


def _matches_request_type(rt: str, prefixes: tuple[str, ...]) -> bool:
    if not prefixes:
        return True
    rt_lc = rt.lower()
    return any(rt_lc.startswith(p) for p in prefixes)


def fetch_code_violations(
    days_back: int = 30,
    request_type_prefixes: tuple[str, ...] = DEFAULT_RT_PREFIXES,
    target_zips: Optional[set[str]] = None,
    max_pages: int = 20,
    per_page: int = 100,
    *,
    open_only: bool = False,
) -> list[HooverSeeClickFixRecord]:
    """Pull Hoover code-enforcement issues from SeeClickFix.

    Args:
        days_back: Cap age of returned issues. SeeClickFix returns newest
            first, so we stop paginating once we cross this threshold.
        request_type_prefixes: Lowercase prefixes to match against
            ``request_type.title``. Default: just "code enforcement"
            (the dedicated category). Pass () to disable filtering.
        target_zips: Optional set of 5-digit ZIPs to keep. Pass
            ``ALL_TARGET`` from ``target_zips`` to filter to Tier 1+2.
        max_pages: Hard cap on pagination.
        per_page: Items per page (SeeClickFix max is 100).
        open_only: If True, drop status="Closed" issues.

    Returns list of HooverSeeClickFixRecord, newest-first, deduped by issue_id.
    """
    session = _new_session()
    seen_ids: set[int] = set()
    out: list[HooverSeeClickFixRecord] = []
    cutoff_age = days_back

    for page in range(1, max_pages + 1):
        params = {
            "lat": HOOVER_CENTER_LAT,
            "lng": HOOVER_CENTER_LNG,
            "zoom": HOOVER_ZOOM,
            "per_page": per_page,
            "page": page,
        }
        try:
            r = session.get(SCF_BASE, params=params)
        except requests.exceptions.RequestException as e:
            logger.warning("SeeClickFix page %d fetch failed: %s", page, e)
            break
        if r.status_code != 200:
            logger.warning("SeeClickFix page %d returned %d", page, r.status_code)
            break
        try:
            data = r.json()
        except ValueError:
            logger.warning("SeeClickFix page %d non-JSON response", page)
            break

        issues = data.get("issues", []) if isinstance(data, dict) else []
        if not issues:
            logger.debug("SeeClickFix page %d: 0 issues — stopping", page)
            break

        page_added = 0
        page_too_old_count = 0
        for issue in issues:
            iid = int(issue.get("id") or 0)
            if iid in seen_ids:
                continue
            seen_ids.add(iid)

            rec = _to_record(issue)
            if rec is None:
                continue

            # Age check — stop pagination if we've drifted past the window
            if rec.age_days > cutoff_age:
                page_too_old_count += 1
                continue

            if not _matches_request_type(rec.request_type, request_type_prefixes):
                continue

            if open_only and rec.status.lower() == "closed":
                continue

            if target_zips and rec.zip not in target_zips:
                continue

            out.append(rec)
            page_added += 1

        logger.info(
            "Hoover SeeClickFix page %d: %d issues seen, %d kept (cumulative: %d)",
            page, len(issues), page_added, len(out),
        )

        # Newest-first: if half of this page is older than cutoff, we're done
        if page_too_old_count > len(issues) // 2:
            logger.debug("  Page %d mostly stale — stopping pagination", page)
            break

        # Throttle — be polite to SeeClickFix
        time.sleep(0.5)

    logger.info("Hoover SeeClickFix: %d code-enforcement issues kept (last %dd)",
                len(out), days_back)
    return out


# ── Owner enrichment ─────────────────────────────────────────────────


def enrich_with_owner(notice) -> bool:
    """Fill ``owner_name`` / ``parcel_id`` via Jefferson property API by
    situs address. Hoover is in Jefferson County, so we use the existing
    `jefferson_property_api.search_by_situs_address()` (E-Ring `searchtype=4`).

    Mirrors `huntsville_unsafe_buildings_api.enrich_with_owner` but routes
    to Jefferson instead of Madison.
    """
    if notice.owner_name:
        return False
    if not notice.address:
        return False

    from jefferson_property_api import search_by_situs_address
    try:
        matches = search_by_situs_address(notice.address)
    except Exception as exc:
        logger.warning("Owner-enrich failed for %r: %s", notice.address, exc)
        return False
    if not matches:
        return False

    # Pick exact-situs match if multiple parcels share the street number
    target = notice.address.upper().strip()
    exact = [m for m in matches if m.situs_address.upper() == target]
    pick = exact[0] if exact else matches[0]

    notice.owner_name = pick.owner_name
    notice.tax_owner_name = pick.owner_name
    if not notice.parcel_id:
        notice.parcel_id = pick.parcel_number
    if not notice.assessed_value and pick.total_value:
        notice.assessed_value = f"{pick.total_value:.0f}"
    if not notice.is_homestead and pick.is_homestead:
        notice.is_homestead = "Y"
    return True


# ── NoticeData conversion ────────────────────────────────────────────


def to_notice_data(
    rec: HooverSeeClickFixRecord, *, enrich_owner: bool = False,
):
    """Convert a SeeClickFix issue → NoticeData for the DataSift pipeline.

    Sets ``notice_type="code_violation"`` and a Hoover-specific
    ``notice_subtype="code_enforcement_complaint"`` to distinguish from
    Huntsville's ``unsafe_building`` (formal condemnation) and from
    Birmingham's other Accela subtypes (housing/vehicles/zoning).
    """
    from notice_parser import NoticeData

    today = date.today().strftime("%Y-%m-%d")
    # SeeClickFix created_at → YYYY-MM-DD
    date_added = today
    if rec.created_at:
        try:
            dt = datetime.fromisoformat(rec.created_at.replace("Z", "+00:00"))
            date_added = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    notice = NoticeData(
        county="Jefferson",
        state="AL",
        notice_type="code_violation",
        # Subtype: citizen-reported complaint (early-stage distress).
        # Different signal class from "unsafe_building" (Huntsville formal
        # condemnation) — softer evidence but earlier in the funnel.
        notice_subtype="code_enforcement_complaint",
        date_added=date_added,
        received_date=today,
        owner_name="",  # filled by enrich_owner if requested
        # Property identity
        address=rec.address,
        city=rec.city,
        zip=rec.zip,
        # SeeClickFix issue ID surfaces in DataSift's "Probate Case Number"
        # column (the only generic case-number slot available — the column
        # name is misleading but the field is the right shape).
        case_number=str(rec.issue_id),
        # Source link — the public SeeClickFix issue page
        source_url=rec.html_url,
        municipality="Hoover",
        raw_text=(
            f"{rec.request_type} — {rec.status} — {rec.summary} "
            f"(SeeClickFix #{rec.issue_id}, opened {date_added}, "
            f"{rec.age_days}d old)"
        ),
    )
    if enrich_owner:
        enrich_with_owner(notice)
    return notice


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Fetch Hoover, AL code-enforcement issues from SeeClickFix.",
    )
    p.add_argument("--days-back", type=int, default=30,
                   help="Cap issue age in days (default: 30)")
    p.add_argument("--max-pages", type=int, default=20,
                   help="SeeClickFix pagination cap (default: 20)")
    p.add_argument("--target-zips-only", action="store_true",
                   help="Filter to our Tier 1/Tier 2 target ZIPs only")
    p.add_argument("--open-only", action="store_true",
                   help="Drop status=Closed issues")
    p.add_argument("--enrich-owners", action="store_true",
                   help="Look up owner of record via Jefferson property API "
                        "(adds ~0.3s per record)")
    p.add_argument("--all-types", action="store_true",
                   help="Don't filter by request_type — pull ALL Hoover issues "
                        "(useful for distribution recon, NOT for production)")
    p.add_argument("--csv-out", type=str, default="",
                   help="Write records to a CSV at this path")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    target_zips = None
    if args.target_zips_only:
        from target_zips import ALL_TARGET
        target_zips = set(ALL_TARGET)

    rt_prefixes = () if args.all_types else DEFAULT_RT_PREFIXES

    records = fetch_code_violations(
        days_back=args.days_back,
        request_type_prefixes=rt_prefixes,
        target_zips=target_zips,
        max_pages=args.max_pages,
        open_only=args.open_only,
    )

    print(f"\nFetched {len(records)} Hoover code-enforcement issue(s) "
          f"(last {args.days_back}d):\n")
    for r in records:
        owner = ""
        if args.enrich_owners:
            from notice_parser import NoticeData
            n = to_notice_data(r, enrich_owner=True)
            if n.owner_name:
                owner = f"  owner: {n.owner_name}"
        print(f"  #{r.issue_id}  {r.status:13}  {r.address}, {r.city} {r.zip}  "
              f"age={r.age_days}d{owner}")
        print(f"    {r.request_type[:80]}")
        print(f"    {r.html_url}")

    if args.csv_out:
        import csv
        from pathlib import Path
        out_path = Path(args.csv_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys())
                                                if records else [])
            writer.writeheader()
            for r in records:
                writer.writerow(r.to_dict())
        print(f"\n✓ Wrote {len(records)} records to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
