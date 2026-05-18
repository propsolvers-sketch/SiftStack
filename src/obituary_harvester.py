"""Harvest fresh Jefferson County obituaries to feed the pre-probate pipeline.

Pulls the legacy.com Birmingham aggregator (which includes AL.com Birmingham
plus other regional papers), extracts individual obituary URLs, and dedupes.
Output is a list of HarvestedObit records ready for the pre-probate
orchestrator to enrich + ZIP-gate + skip-trace.

The hard part of obituary harvesting is anti-bot — legacy.com is a React
SPA, AL.com obit listing returns HTTP 403 to plain requests. We route the
listing fetch through Firecrawl (which renders JS and bypasses the 403) using
the existing infrastructure in obituary_enricher._fetch_firecrawl().

Public API:

    harvest_birmingham(limit: int = 50) -> list[HarvestedObit]

V1 covers Birmingham/Jefferson only. Madison (Huntsville) listing is at
``https://www.legacy.com/us/obituaries/local/alabama/huntsville`` — same
shape, can be added later. Only Jefferson is wired today because Madison
post-probate isn't supported either.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import asdict, dataclass

from dotenv import load_dotenv

from obituary_enricher import _fetch_firecrawl, _fetch_page_text

load_dotenv()

logger = logging.getLogger(__name__)


# ── Listing-page sources ─────────────────────────────────────────────


# legacy.com Birmingham — primary Jefferson-area aggregator. Includes AL.com
# Birmingham (the dominant Jefferson-area paper) plus most Birmingham-area
# funeral homes that syndicate obituaries.
BIRMINGHAM_LISTING_URL = "https://www.legacy.com/us/obituaries/local/alabama/birmingham"

# legacy.com Huntsville — Madison/Limestone-area aggregator. Includes
# AL.com Huntsville and Huntsville-area funeral homes.
HUNTSVILLE_LISTING_URL = "https://www.legacy.com/us/obituaries/local/alabama/huntsville"
# Marshall County aggregator listing — covers Albertville, Boaz, Guntersville,
# Arab and surrounding communities. legacy.com publishes a county-level
# aggregate page distinct from the city-level Birmingham/Huntsville pages.
MARSHALL_LISTING_URL = "https://www.legacy.com/us/obituaries/local/alabama/marshall-county"

# Mapping: market_label → (listing_url, county_hint). The county_hint is
# the most likely property-API county for obits from that listing — used
# by the orchestrator to prioritize which county API to query first.
ALABAMA_LISTINGS: dict[str, tuple[str, str]] = {
    "Birmingham": (BIRMINGHAM_LISTING_URL, "Jefferson"),
    "Huntsville": (HUNTSVILLE_LISTING_URL, "Madison"),
    "Marshall": (MARSHALL_LISTING_URL, "Marshall"),
}

# Legacy.com paginates with a `page=N` query param (1-indexed). The first
# page typically returns ~25-50 entries depending on volume.

# Regex to extract individual obituary URLs from the rendered listing markdown.
# Two formats co-exist: legacy.com person URLs (the canonical record) and
# obits.al.com URLs (the syndicated copy on AL.com). We prefer legacy.com
# because it has richer JSON-LD; the AL.com URL is a fallback.
LEGACY_PERSON_URL_RE = re.compile(
    r"https?://www\.legacy\.com/person/([\w\-]+?)-(\d+)",
)

# Legacy.com market-named obit URL — used by the Huntsville/Marshall listings
# as the DOMINANT format. Pattern: legacy.com/us/obituaries/<market>/name/<slug>-obituary?id=<id>
# Market segment is any lowercase slug ("huntsville", "birmingham", "vnews"
# for vendor-syndicated entries, individual city slugs like "albertville" for
# Marshall County, etc.). Since the obit URLs are only matched against text
# fetched from a specific county/market listing page, broadening the slug
# pattern doesn't introduce cross-market noise — it just lets us add new
# markets without code changes. These pages render the full obituary inline,
# no URL upgrade needed.
LEGACY_MARKET_URL_RE = re.compile(
    r"https?://www\.legacy\.com/us/obituaries/([a-z0-9\-]+)/name/([\w\-]+?)-obituary\?id=(\d+)",
)

# AL.com Birmingham individual obit URL — fallback when no legacy.com entry
ALCOM_OBIT_URL_RE = re.compile(
    r"https?://obits\.al\.com/us/obituaries/birmingham/name/([\w\-]+?)-obituary\?id=(\d+)",
)

# A name token preceding the URL is often the decedent's display name in
# the listing markdown ("[John Doe](https://www.legacy.com/person/...)").
NAMED_LEGACY_LINK_RE = re.compile(
    r"\[\*?\*?([^\]]+?)\*?\*?\]\((https?://www\.legacy\.com/(?:person/[\w\-]+?-\d+|us/obituaries/(?:huntsville|birmingham|vnews)/name/[\w\-]+?-obituary\?id=\d+))\)",
)


@dataclass
class HarvestedObit:
    """One obituary URL discovered in a listing page.

    `name_hint` is the display text from the listing link (e.g. "Annie Laurie
    Schapmann") — useful as a quick-reject filter before paying the LLM call.
    The full decedent name comes from the per-obit page LLM extraction
    downstream. `county_hint` records the most likely property-API county
    for this obit (Jefferson for Birmingham listing, Madison for Huntsville).
    """

    url: str
    source: str           # "legacy.com" or "obits.al.com"
    name_hint: str = ""
    listing_url: str = ""
    county_hint: str = ""  # "Jefferson" | "Madison" | "" — used by orchestrator routing

    def to_dict(self) -> dict:
        return asdict(self)


# ── Harvester ────────────────────────────────────────────────────────


def _fetch_listing_text(url: str, max_chars: int = 60000) -> str:
    """Get the full listing-page text. Tries Firecrawl first (handles JS +
    anti-bot in one call) and falls back to ``_fetch_page_text`` if Firecrawl
    is unavailable.
    """
    text = _fetch_firecrawl(url, wait_ms=4000, max_text=max_chars)
    if text and len(text) >= 1000:
        return text
    logger.debug("Firecrawl returned short/empty listing — falling back to direct fetch")
    return _fetch_page_text(url)


def harvest_listing(
    listing_url: str,
    county_hint: str = "",
    limit: int = 50,
    pages: int = 1,
    seen_keys: set[tuple[str, str]] | None = None,
) -> list[HarvestedObit]:
    """Pull obituary URLs from a single legacy.com regional listing.

    Args:
        listing_url: e.g. ``BIRMINGHAM_LISTING_URL`` or ``HUNTSVILLE_LISTING_URL``.
        county_hint: "Jefferson" | "Madison" | "" — annotated onto each
            HarvestedObit so the orchestrator can prioritize the right
            property-API county first.
        limit: Hard cap on URLs returned.
        pages: How many listing pages to walk (1-indexed).
        seen_keys: Optional dedupe set shared across multiple harvest calls.
            (source, id) tuples — pass when harvesting multiple listings to
            dedupe across them.

    Returns:
        Deduplicated list of HarvestedObit, in listing order (newest first).
    """
    if seen_keys is None:
        seen_keys = set()
    results: list[HarvestedObit] = []

    # The AL.com URL pattern uses the market segment as a path component
    # ("birmingham" or "huntsville"). Detect it from the listing URL.
    if "huntsville" in listing_url:
        alcom_pattern = re.compile(
            r"https?://obits\.al\.com/us/obituaries/huntsville/name/([\w\-]+?)-obituary\?id=(\d+)",
        )
        alcom_market = "huntsville"
    else:
        alcom_pattern = ALCOM_OBIT_URL_RE
        alcom_market = "birmingham"

    for p in range(1, pages + 1):
        page_url = listing_url if p == 1 else f"{listing_url}?page={p}"
        logger.info("Harvesting page %d: %s", p, page_url)
        text = _fetch_listing_text(page_url)
        if not text:
            logger.warning("Empty listing page %d — stopping early", p)
            break

        # Build name-hint map first by walking [name](url) markdown links
        name_by_url: dict[str, str] = {}
        for m in NAMED_LEGACY_LINK_RE.finditer(text):
            name_by_url.setdefault(m.group(2), m.group(1).strip())

        # Pull legacy.com person URLs (Birmingham listing's primary format)
        for m in LEGACY_PERSON_URL_RE.finditer(text):
            url = f"https://www.legacy.com/person/{m.group(1)}-{m.group(2)}"
            key = ("legacy.com", m.group(2))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(HarvestedObit(
                url=url, source="legacy.com",
                name_hint=name_by_url.get(url, ""),
                listing_url=page_url,
                county_hint=county_hint,
            ))
            if len(results) >= limit:
                logger.info("Hit limit %d on page %d — stopping", limit, p)
                return results

        # Pull legacy.com /us/obituaries/<market>/name/... URLs (Huntsville
        # listing's primary format; also some Birmingham syndicated entries).
        # These render the full obituary inline — no URL upgrade needed.
        for m in LEGACY_MARKET_URL_RE.finditer(text):
            market_seg = m.group(1)  # "huntsville" | "birmingham" | "vnews"
            slug = m.group(2)
            obit_id = m.group(3)
            url = f"https://www.legacy.com/us/obituaries/{market_seg}/name/{slug}-obituary?id={obit_id}"
            source_label = f"legacy.com.{market_seg}"
            key = (source_label, obit_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(HarvestedObit(
                url=url, source=source_label,
                name_hint=name_by_url.get(url, ""),
                listing_url=page_url,
                county_hint=county_hint,
            ))
            if len(results) >= limit:
                logger.info("Hit limit %d on page %d — stopping", limit, p)
                return results

        # Pull AL.com URLs as fallback (different market = different URL pattern)
        for m in alcom_pattern.finditer(text):
            url = f"https://obits.al.com/us/obituaries/{alcom_market}/name/{m.group(1)}-obituary?id={m.group(2)}"
            key = ("obits.al.com", m.group(2))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(HarvestedObit(
                url=url, source="obits.al.com",
                name_hint="",
                listing_url=page_url,
                county_hint=county_hint,
            ))
            if len(results) >= limit:
                logger.info("Hit limit %d on page %d — stopping", limit, p)
                return results

        logger.info("Page %d: %d cumulative obit URLs", p, len(results))

    return results


def harvest_birmingham(
    limit: int = 50,
    listing_url: str = BIRMINGHAM_LISTING_URL,
    pages: int = 1,
) -> list[HarvestedObit]:
    """Backwards-compat wrapper — Birmingham/Jefferson only."""
    return harvest_listing(
        listing_url=listing_url, county_hint="Jefferson",
        limit=limit, pages=pages,
    )


def harvest_huntsville(
    limit: int = 50,
    listing_url: str = HUNTSVILLE_LISTING_URL,
    pages: int = 1,
) -> list[HarvestedObit]:
    """Madison/Huntsville obit listing harvester."""
    return harvest_listing(
        listing_url=listing_url, county_hint="Madison",
        limit=limit, pages=pages,
    )


def harvest_alabama(
    markets: tuple[str, ...] = ("Birmingham", "Huntsville"),
    limit_per_market: int = 50,
    pages: int = 1,
) -> list[HarvestedObit]:
    """Harvest both Birmingham + Huntsville (or any subset of Alabama markets).

    Dedupes across markets — same legacy.com person ID wouldn't appear on
    both listings, but the shared seen_keys set is defensive.
    """
    seen_keys: set[tuple[str, str]] = set()
    results: list[HarvestedObit] = []
    for market in markets:
        if market not in ALABAMA_LISTINGS:
            logger.warning("Unknown market %r — skipping", market)
            continue
        url, county = ALABAMA_LISTINGS[market]
        market_obits = harvest_listing(
            listing_url=url, county_hint=county,
            limit=limit_per_market, pages=pages, seen_keys=seen_keys,
        )
        results.extend(market_obits)
        logger.info("Market %s: %d unique obits (cumulative: %d)",
                    market, len(market_obits), len(results))
    return results


# ── CLI ──────────────────────────────────────────────────────────────


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Harvest fresh Alabama obituary URLs from legacy.com (Birmingham + Huntsville).",
    )
    p.add_argument("--markets", type=str, default="Birmingham,Huntsville",
                   help="Comma-separated markets (default: Birmingham,Huntsville). "
                        "Choices: Birmingham, Huntsville.")
    p.add_argument("--limit", type=int, default=50,
                   help="Max obituary URLs per market (default: 50)")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to walk per market (default: 1)")
    p.add_argument("--json", action="store_true",
                   help="Output JSON list instead of pretty summary")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "h2", "hpack", "primp", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())
    obits = harvest_alabama(markets=markets, limit_per_market=args.limit, pages=args.pages)

    if args.json:
        import json
        print(json.dumps([o.to_dict() for o in obits], indent=2))
    else:
        print(f"\nHarvested {len(obits)} obituary URL(s):\n")
        for o in obits:
            name = o.name_hint or "(name unknown — needs LLM extraction)"
            tag = f"{o.source}·{o.county_hint}" if o.county_hint else o.source
            print(f"  [{tag:24}]  {name}")
            print(f"      {o.url}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
