"""Scrape Zillow's pending-listings-only search results for a ZIP.

OpenWeb Ninja's /search endpoint strips contingent/pending listings (Zillow's
default search behavior). To recover them we hit Zillow's pending-only filter
URL directly via Playwright: `https://www.zillow.com/{zip}/pending_listing_type/`.

The page embeds a `__NEXT_DATA__` JSON blob containing the structured listing
data — far more reliable than DOM card parsing. Falls back to DOM parsing if
the embedded JSON schema shifts.

Returns dicts shaped like comp_analyzer.fetch_pending_sales output so it can
slot in transparently:
    {address, city, zip_code, list_price, sqft, bedrooms, bathrooms,
     year_built, ppsf, distance_miles, days_on_market, detail_url, status}
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Any

from playwright.async_api import TimeoutError as PwTimeout, async_playwright
from playwright_stealth import Stealth

import config

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_PENDING_URL = "https://www.zillow.com/{zip_code}/pending_listing_type/"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.DOTALL,
)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lng points, in miles."""
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


_STEALTH_INIT = """
() => {
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // Spoof plugins/languages
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    // Spoof platform + hardware
    Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    // Chrome runtime
    window.chrome = { runtime: {} };
    // Permissions API consistency
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(p);
}
"""


def _build_proxy_config(proxy_url: str) -> dict | None:
    """Parse `http://user:pass@host:port` into Playwright's proxy dict shape."""
    if not proxy_url:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return None
    cfg: dict = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


async def _scrape_zip(zip_code: str, headless: bool = True, proxy_url: str = "") -> list[dict]:
    """Async core: navigate, extract __NEXT_DATA__, parse listResults."""
    async with Stealth().use_async(async_playwright()) as p:
        # Real-browser-ish launch flags to reduce headless detection
        launch_opts: dict = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
            ],
        }
        proxy_cfg = _build_proxy_config(proxy_url)
        if proxy_cfg:
            launch_opts["proxy"] = proxy_cfg
            logger.info("Zillow scrape routing via proxy %s", proxy_cfg["server"])
        browser = await p.chromium.launch(**launch_opts)
        ctx = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Upgrade-Insecure-Requests": "1",
            },
        )
        # Inject stealth shim on every page before any script runs
        await ctx.add_init_script(_STEALTH_INIT)
        ctx.set_default_timeout(45_000)
        page = await ctx.new_page()
        url = _PENDING_URL.format(zip_code=zip_code)
        listings: list[dict] = []
        try:
            # Warm-up: visit Zillow homepage first so we get a real session cookie,
            # then navigate to the pending-listings URL. The homepage rarely
            # CAPTCHAs and gives us a real `zguid` cookie that the search page
            # respects.
            await page.goto("https://www.zillow.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            html_head = (await page.content())[:5000].lower()
            if "press & hold" in html_head or "captcha" in html_head or "are you a human" in html_head:
                logger.warning("Zillow CAPTCHA wall hit for ZIP %s — pending scrape blocked", zip_code)
                return []

            # Wait briefly for the SPA to hydrate the listings panel
            try:
                await page.wait_for_selector('[data-test="property-card"]', timeout=15_000)
            except PwTimeout:
                # Even without cards, __NEXT_DATA__ may already be populated
                pass

            html = await page.content()
            m = _NEXT_DATA_RE.search(html)
            if not m:
                logger.warning("No __NEXT_DATA__ found for ZIP %s (page layout changed?)", zip_code)
                return []
            try:
                next_data = json.loads(m.group(1))
            except json.JSONDecodeError as e:
                logger.warning("Failed to parse __NEXT_DATA__ for ZIP %s: %s", zip_code, e)
                return []

            listings = _extract_listings_from_next_data(next_data)
            logger.info("Zillow pending scrape for ZIP %s returned %d listings", zip_code, len(listings))
        except Exception as e:
            logger.warning("Zillow pending scrape failed for ZIP %s: %s", zip_code, e)
        finally:
            await ctx.close()
            await browser.close()
        return listings


def _extract_listings_from_next_data(next_data: dict) -> list[dict]:
    """Walk Zillow's __NEXT_DATA__ blob to the listResults array."""
    try:
        page_props = next_data.get("props", {}).get("pageProps", {})
        # Two known shapes — newer "cat1" wrapper and older flat shape
        cat1 = page_props.get("searchPageState", {}).get("cat1") or page_props.get("cat1") or {}
        list_results = cat1.get("searchResults", {}).get("listResults") or []
        if not list_results:
            # Try the alternate "componentProps" path used on some variants
            comp_props = next_data.get("props", {}).get("pageProps", {}).get("componentProps", {})
            list_results = (
                comp_props.get("searchPageState", {})
                .get("cat1", {})
                .get("searchResults", {})
                .get("listResults", [])
            )
    except (AttributeError, TypeError) as e:
        logger.warning("Unexpected __NEXT_DATA__ shape: %s", e)
        return []
    return list_results


def _normalize_listing(raw: dict, subject_lat: float, subject_lon: float,
                       radius_miles: float) -> dict | None:
    """Map a Zillow listResults entry to our pending_comps dict shape."""
    if not isinstance(raw, dict):
        return None
    hi = raw.get("hdpData", {}).get("homeInfo") or {}
    price = (
        raw.get("unformattedPrice")
        or hi.get("price")
        or raw.get("price")
        or 0
    )
    try:
        price = int(price)
    except (TypeError, ValueError):
        return None
    if price < 10_000 or price > 50_000_000:
        return None
    sqft = raw.get("area") or hi.get("livingArea") or 0
    try:
        sqft = int(sqft)
    except (TypeError, ValueError):
        sqft = 0
    if not sqft:
        return None

    addr_obj = raw.get("addressStreet") or raw.get("address") or hi.get("streetAddress") or ""
    if isinstance(addr_obj, dict):
        street = addr_obj.get("streetAddress") or ""
        city = addr_obj.get("city") or hi.get("city") or ""
        zipc = addr_obj.get("zipcode") or hi.get("zipcode") or ""
    else:
        street = addr_obj or raw.get("streetAddress") or ""
        city = raw.get("addressCity") or hi.get("city") or ""
        zipc = raw.get("addressZipcode") or hi.get("zipcode") or ""

    lat = float(raw.get("latitude") or hi.get("latitude") or (raw.get("latLong") or {}).get("latitude") or 0)
    lon = float(raw.get("longitude") or hi.get("longitude") or (raw.get("latLong") or {}).get("longitude") or 0)
    dist = 0.0
    if subject_lat and subject_lon and lat and lon:
        dist = _haversine_miles(subject_lat, subject_lon, lat, lon)
        if dist > radius_miles:
            return None

    dom_raw = raw.get("daysOnZillow") or hi.get("daysOnZillow") or 0
    try:
        dom = int(dom_raw) if dom_raw < 100_000 else int(dom_raw / 86_400_000)
    except (TypeError, ValueError):
        dom = 0

    detail_url = raw.get("detailUrl") or ""
    if detail_url and detail_url.startswith("/"):
        detail_url = f"https://www.zillow.com{detail_url}"

    contingent = (
        raw.get("contingentListingType")
        or hi.get("contingentListingType")
        or "PENDING"
    )
    status_text = raw.get("statusText") or hi.get("statusText") or contingent.title()

    return {
        "address": street,
        "city": city,
        "zip_code": zipc,
        "list_price": price,
        "sqft": sqft,
        "bedrooms": raw.get("beds") or raw.get("bedrooms") or hi.get("bedrooms") or 0,
        "bathrooms": raw.get("baths") or raw.get("bathrooms") or hi.get("bathrooms") or 0,
        "year_built": int(hi.get("yearBuilt") or raw.get("yearBuilt") or 0),
        "ppsf": round(price / sqft, 2) if sqft else 0.0,
        "distance_miles": round(dist, 2),
        "days_on_market": dom,
        "detail_url": detail_url,
        "status": status_text,
        "contingent_type": contingent,
    }


def fetch_pending_via_zillow(zip_code: str, subject_lat: float = 0.0,
                              subject_lon: float = 0.0,
                              radius_miles: float = 2.0,
                              max_results: int = 6,
                              headless: bool = True,
                              proxy_url: str = "") -> list[dict]:
    """Sync wrapper — returns up to N pending/contingent listings near subject.

    Sorted by proximity (closer first). Each dict carries the standard pending
    comp shape so callers can pass it through unchanged.

    Uses `config.ZILLOW_PROXY_URL` (Webshare residential) by default — without
    a residential IP, Zillow's PerimeterX blocks the scrape immediately.

    On CAPTCHA wall, layout change, or any scrape failure: returns [] and logs
    a warning. Never raises — the caller treats empty as "no data available".
    """
    if not zip_code:
        return []
    proxy_url = proxy_url or config.ZILLOW_PROXY_URL
    if not proxy_url:
        logger.info("ZILLOW_PROXY_URL not set — skipping Zillow pending scrape (would be blocked by PerimeterX)")
        return []
    try:
        raw_listings = asyncio.run(_scrape_zip(zip_code, headless=headless, proxy_url=proxy_url))
    except RuntimeError:
        # Caller already inside an asyncio loop — fall back to a fresh loop
        loop = asyncio.new_event_loop()
        try:
            raw_listings = loop.run_until_complete(_scrape_zip(zip_code, headless=headless, proxy_url=proxy_url))
        finally:
            loop.close()

    normalized = [
        n for n in (
            _normalize_listing(r, subject_lat, subject_lon, radius_miles)
            for r in raw_listings
        )
        if n
    ]
    # Sort: closest first
    normalized.sort(key=lambda p: p["distance_miles"])
    return normalized[:max_results]


if __name__ == "__main__":
    # CLI smoke test: python src/zillow_pending_scraper.py 35071
    import sys
    logging.basicConfig(level=logging.INFO)
    zc = sys.argv[1] if len(sys.argv) > 1 else "35071"
    results = fetch_pending_via_zillow(zc, headless=True, max_results=10)
    print(f"\nGot {len(results)} pending listings for ZIP {zc}:\n")
    for r in results:
        print(f"  {r['address']:35s} ${r['list_price']:>10,}  "
              f"{r['sqft']:>5,} sqft  {r['bedrooms']}bd/{r['bathrooms']}ba  "
              f"DOM {r['days_on_market']:>4d}  {r['status']}")
        print(f"    {r['detail_url']}")
