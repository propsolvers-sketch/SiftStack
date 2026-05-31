"""Scrape Redfin's pending + contingent listings via their public CSV API.

Redfin exposes a clean CSV download endpoint (`/stingray/api/gis-csv`) that
returns all currently-listed properties in a region with their MLS-direct
status (Active, Pending, Contingent, Coming Soon). This is dramatically more
reliable than Zillow scraping — Redfin's anti-bot is much lighter, and the
CSV path doesn't require a browser at all.

Flow:
  1. Resolve ZIP → Redfin internal region_id by scraping the public ZIP page
     and grepping the embedded `region_id=NNNN` reference.
  2. Call the CSV API with status bitmask 11 (Active + Pending + Contingent)
     and include_pending_homes=true.
  3. Parse the CSV, keep only Pending + Contingent rows, dedupe, sort by
     proximity to subject.

Returns dicts shaped like comp_analyzer.fetch_pending_sales output for
drop-in replacement of the Zillow scraper.

Requires `ZILLOW_PROXY_URL` in .env (Webshare residential US) — Redfin lightly
rate-limits raw IPs, but residential traffic flows through with no challenge.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import random
import re
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_ZIP_PAGE = "https://www.redfin.com/zipcode/{zip_code}"
_CSV_API = (
    "https://www.redfin.com/stingray/api/gis-csv"
    "?al=1&include_pending_homes=true&num_homes=350"
    "&region_id={region_id}&region_type=2"
    "&sf=1,2,3,5,6,7&status=11&uipt=1,2,3,4,5,6,7,8&v=8"
)
_CSV_API_SOLD = (
    "https://www.redfin.com/stingray/api/gis-csv"
    "?al=1&include_sold_homes=true&sold_within_days={days}&num_homes=350"
    "&region_id={region_id}&region_type=2"
    "&sf=1,2,3,5,6,7&status=4&uipt=1,2,3,4,5,6,7,8&v=8"
)
# status bitmask: 1=Active + 2=Pending + 8=Contingent = 11. status=4 means Sold.
_REGION_ID_RE = re.compile(r"regionId=(\d+)|region_id=(\d+)")


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _proxy_dict() -> dict | None:
    proxy_url = config.ZILLOW_PROXY_URL
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _resolve_region_id(zip_code: str) -> int | None:
    """Look up Redfin's internal region_id for a 5-digit US ZIP.

    Scrapes the public ZIP page (which always loads, no auth) and greps for
    the `regionId=NNNN` reference in the embedded React state.
    """
    proxies = _proxy_dict()
    try:
        r = requests.get(_ZIP_PAGE.format(zip_code=zip_code),
                         headers=_HEADERS, proxies=proxies, timeout=20)
    except Exception as e:
        logger.warning("Redfin ZIP-page fetch failed for %s: %s", zip_code, e)
        return None
    if r.status_code != 200:
        logger.warning("Redfin ZIP page %s returned HTTP %d", zip_code, r.status_code)
        return None
    matches = _REGION_ID_RE.findall(r.text)
    # _REGION_ID_RE returns tuples (group1, group2) — flatten and pick the most common
    candidates = [m[0] or m[1] for m in matches if (m[0] or m[1])]
    if not candidates:
        logger.warning("No region_id found on Redfin ZIP page %s", zip_code)
        return None
    # Redfin sometimes leaks the ZIP itself as a number — pick the most-frequent that isn't the ZIP
    from collections import Counter
    counts = Counter(candidates)
    for cid, _ in counts.most_common():
        if cid != zip_code:
            return int(cid)
    return None


def _parse_listing_row(row: dict, subject_lat: float, subject_lon: float,
                       radius_miles: float) -> dict | None:
    """Map a Redfin CSV row to our pending_comps dict shape."""
    status = (row.get("STATUS") or "").strip()
    if status not in ("Pending", "Contingent"):
        return None
    try:
        price = int(float(row.get("PRICE") or 0))
    except (TypeError, ValueError):
        return None
    if price < 10_000 or price > 50_000_000:
        return None
    try:
        sqft = int(float(row.get("SQUARE FEET") or 0))
    except (TypeError, ValueError):
        sqft = 0
    if not sqft:
        return None

    try:
        lat = float(row.get("LATITUDE") or 0)
        lon = float(row.get("LONGITUDE") or 0)
    except (TypeError, ValueError):
        lat = lon = 0.0
    dist = 0.0
    if subject_lat and subject_lon and lat and lon:
        dist = _haversine_miles(subject_lat, subject_lon, lat, lon)
        if dist > radius_miles:
            return None

    # Detail URL column name has parenthetical noise — pick the column whose value starts with https
    detail_url = ""
    for k, v in row.items():
        if k.startswith("URL") and isinstance(v, str) and v.startswith("http"):
            detail_url = v
            break

    try:
        dom = int(float(row.get("DAYS ON MARKET") or 0))
    except (TypeError, ValueError):
        dom = 0
    try:
        bedrooms = int(float(row.get("BEDS") or 0))
    except (TypeError, ValueError):
        bedrooms = 0
    try:
        bathrooms = float(row.get("BATHS") or 0)
    except (TypeError, ValueError):
        bathrooms = 0
    try:
        year_built = int(float(row.get("YEAR BUILT") or 0))
    except (TypeError, ValueError):
        year_built = 0
    try:
        ppsf = round(float(row.get("$/SQUARE FEET") or 0), 2)
    except (TypeError, ValueError):
        ppsf = round(price / sqft, 2) if sqft else 0.0

    return {
        "address": (row.get("ADDRESS") or "").strip(),
        "city": (row.get("CITY") or "").strip(),
        "zip_code": (row.get("ZIP OR POSTAL CODE") or "").strip(),
        "list_price": price,
        "sqft": sqft,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "year_built": year_built,
        "ppsf": ppsf,
        "distance_miles": round(dist, 2),
        "days_on_market": dom,
        "detail_url": detail_url,
        "status": status,            # "Pending" or "Contingent"
        "contingent_type": status.upper(),
        "source": "redfin",
        "mls_number": (row.get("MLS#") or "").strip(),
    }


def fetch_pending_via_redfin(zip_code: str, subject_lat: float = 0.0,
                              subject_lon: float = 0.0,
                              radius_miles: float = 2.0,
                              max_results: int = 6) -> list[dict]:
    """Returns up to N Pending + Contingent listings near subject via Redfin CSV API.

    Sorted by proximity (closer first). On any failure (proxy down, region
    lookup miss, CSV parse error), returns [] and logs a warning. Never raises.
    """
    if not zip_code:
        return []
    if not config.ZILLOW_PROXY_URL:
        logger.info("ZILLOW_PROXY_URL not set — skipping Redfin pending scrape")
        return []

    region_id = _resolve_region_id(zip_code)
    if not region_id:
        return []

    # Brief delay between region-lookup and CSV-fetch to avoid pinging from a single IP back-to-back
    time.sleep(random.uniform(0.5, 1.5))

    try:
        r = requests.get(
            _CSV_API.format(region_id=region_id),
            headers=_HEADERS, proxies=_proxy_dict(), timeout=25,
        )
    except Exception as e:
        logger.warning("Redfin CSV API fetch failed for region %d: %s", region_id, e)
        return []
    if r.status_code != 200:
        logger.warning("Redfin CSV API returned HTTP %d for region %d", r.status_code, region_id)
        return []
    rows = list(csv.DictReader(io.StringIO(r.text)))
    logger.info("Redfin CSV for ZIP %s (region %d) returned %d total listings",
                zip_code, region_id, len(rows))

    normalized = [
        p for p in (
            _parse_listing_row(row, subject_lat, subject_lon, radius_miles)
            for row in rows
        )
        if p
    ]
    normalized.sort(key=lambda p: p["distance_miles"])
    logger.info("Kept %d Pending/Contingent within %.1f mi", len(normalized), radius_miles)
    return normalized[:max_results]


def _parse_sold_row(row: dict, subject_lat: float, subject_lon: float,
                    radius_miles: float) -> dict | None:
    """Map a Redfin sold-listing CSV row to a sold-comp dict for ARV calc.

    Shape matches what comp_analyzer expects to upgrade into a CompProperty.
    """
    status = (row.get("STATUS") or "").strip()
    if status not in ("Sold",):
        return None
    try:
        sold_price = int(float(row.get("PRICE") or 0))
    except (TypeError, ValueError):
        return None
    if sold_price < 10_000:
        return None
    try:
        sqft = int(float(row.get("SQUARE FEET") or 0))
    except (TypeError, ValueError):
        sqft = 0
    if not sqft:
        return None

    try:
        lat = float(row.get("LATITUDE") or 0)
        lon = float(row.get("LONGITUDE") or 0)
    except (TypeError, ValueError):
        lat = lon = 0.0
    dist = 0.0
    if subject_lat and subject_lon and lat and lon:
        dist = _haversine_miles(subject_lat, subject_lon, lat, lon)
        if dist > radius_miles:
            return None

    detail_url = ""
    for k, v in row.items():
        if k.startswith("URL") and isinstance(v, str) and v.startswith("http"):
            detail_url = v
            break

    try:
        bedrooms = int(float(row.get("BEDS") or 0))
    except (TypeError, ValueError):
        bedrooms = 0
    try:
        bathrooms = float(row.get("BATHS") or 0)
    except (TypeError, ValueError):
        bathrooms = 0
    try:
        year_built = int(float(row.get("YEAR BUILT") or 0))
    except (TypeError, ValueError):
        year_built = 0
    try:
        ppsf = round(float(row.get("$/SQUARE FEET") or 0), 2)
    except (TypeError, ValueError):
        ppsf = round(sold_price / sqft, 2) if sqft else 0.0

    return {
        "address": (row.get("ADDRESS") or "").strip(),
        "city": (row.get("CITY") or "").strip(),
        "state": (row.get("STATE OR PROVINCE") or "").strip(),
        "zip_code": (row.get("ZIP OR POSTAL CODE") or "").strip(),
        "latitude": lat,
        "longitude": lon,
        "sold_price": sold_price,
        "sold_date": (row.get("SOLD DATE") or "").strip(),
        "sqft": sqft,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "year_built": year_built,
        "ppsf": ppsf,
        "distance_miles": round(dist, 2),
        "detail_url": detail_url,
        "source": "redfin",
        "mls_number": (row.get("MLS#") or "").strip(),
        "property_type": (row.get("PROPERTY TYPE") or "").strip(),
    }


def fetch_sold_comps_via_redfin(zip_code: str, subject_lat: float = 0.0,
                                 subject_lon: float = 0.0,
                                 radius_miles: float = 1.0,
                                 months_back: int = 6,
                                 max_results: int = 30) -> list[dict]:
    """Returns sold listings near subject from Redfin's MLS-direct feed.

    Used alongside Zillow's RECENTLY_SOLD comps to expand the candidate pool
    before similarity ranking. Returns dicts shaped for direct CompProperty
    construction in comp_analyzer (sold_price, sold_date, lat/lon, etc.).
    """
    if not zip_code or not config.ZILLOW_PROXY_URL:
        return []
    region_id = _resolve_region_id(zip_code)
    if not region_id:
        return []
    time.sleep(random.uniform(0.5, 1.5))

    days = months_back * 30
    try:
        r = requests.get(
            _CSV_API_SOLD.format(region_id=region_id, days=days),
            headers=_HEADERS, proxies=_proxy_dict(), timeout=25,
        )
    except Exception as e:
        logger.warning("Redfin sold-CSV fetch failed for region %d: %s", region_id, e)
        return []
    if r.status_code != 200:
        logger.warning("Redfin sold-CSV returned HTTP %d for region %d", r.status_code, region_id)
        return []
    rows = list(csv.DictReader(io.StringIO(r.text)))
    logger.info("Redfin SOLD CSV for ZIP %s (region %d, %dd window) returned %d rows",
                zip_code, region_id, days, len(rows))

    normalized = [
        p for p in (
            _parse_sold_row(row, subject_lat, subject_lon, radius_miles)
            for row in rows
        )
        if p
    ]
    normalized.sort(key=lambda p: p["distance_miles"])
    logger.info("Kept %d Redfin sold comps within %.1f mi / %dmo",
                len(normalized), radius_miles, months_back)
    return normalized[:max_results]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    zc = sys.argv[1] if len(sys.argv) > 1 else "35071"
    mode = sys.argv[2] if len(sys.argv) > 2 else "pending"
    if mode == "sold":
        results = fetch_sold_comps_via_redfin(zc, radius_miles=1.0, months_back=6, max_results=30)
        print(f"\nGot {len(results)} sold comps for ZIP {zc} (1mi/6mo):\n")
        for r in results:
            print(f"  Sold {r['sold_date']:10s} | {r['address']:35s} | ${r['sold_price']:>10,} | "
                  f"{r['sqft']:>5,} sf | yr {r['year_built']} | {r['distance_miles']:.2f} mi")
    else:
        results = fetch_pending_via_redfin(zc, max_results=20)
        print(f"\nGot {len(results)} pending/contingent listings for ZIP {zc}:\n")
        for r in results:
            print(f"  {r['status']:11s} | {r['address']:35s} | ${r['list_price']:>10,} | "
                  f"{r['sqft']:>5,} sf | {r['bedrooms']}bd/{r['bathrooms']}ba | "
                  f"yr {r['year_built']} | DOM {r['days_on_market']:>4d}")
            if r['detail_url']:
                print(f"    {r['detail_url']}")
