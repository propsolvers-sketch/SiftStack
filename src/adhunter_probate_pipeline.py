"""TN Valley Media AdHunter probate notice adapter.

Scrapes the AdHunter classifieds platform at
    https://classads.tnvalleymedia.com/AdHunter/Default/Home/Search?majorClass=10
which aggregates public notices from 9 papers under TN Valley Media:

    Decatur Daily, Times Daily (Shoals), Courier Journal (Shoals),
    Moulton Advertiser, Advertiser-Gleam Classifieds, Redstone Rocket
    Classifieds, Franklin County Times, Hartselle Enquirer,
    The Madison Record.

We filter to probate notices published in Madison or Marshall County, AL:

    * The Madison Record       → Madison County estates (closes APN gap)
    * Advertiser-Gleam         → Marshall County estates (complements APN)
    * Redstone Rocket          → Madison-area military estates (edge cases)

Motivation: alabamapublicnotices.com (APN) returns ~2-4 Madison + ~6
Marshall probate notices per weekly CSV. Population math says Madison
should be running ~4x Marshall — the inversion pointed to a publication
gap. Research 2026-07-09 confirmed:

    * The Madison Record is NOT an APN participating publication
      → most Madison probate notices are invisible to us via APN.
    * Advertiser-Gleam IS an APN participant but the AdHunter platform
      surfaces additional Marshall estates APN misses.

Portal shape (verified 2026-07-09):

    * Server-rendered HTML, no CAPTCHA, no login, no JavaScript.
    * Notice list = <a href="/AdHunter/Default/Home/Ad/{id}"> with
      <h4> title + teaser text + publication-date range + source-paper
      domain (e.g. "advertisergleam.com", "themadisonrecord.com").
    * Pagination: ?page=N&perpage=100&majorClass=10&minorClass=-1
    * Detail page: /AdHunter/Default/Home/Ad/{id}

Pipeline flow:

    1. Fetch listing pages (paginate to end)
    2. Title pre-filter: keep only "IN THE PROBATE COURT" /
       "IN THE MATTER OF THE ESTATE" / "NOTICE TO CREDITORS" patterns
    3. Cross-run dedup by AdHunter ad_id
       (.adhunter_probate_seen_ids.json, 30-day prune window)
    4. Fetch detail pages for surviving candidates
    5. Extract decedent name, PR name, case #, granted date, county
    6. County filter: keep only Madison + Marshall
    7. Route through probate_property_locator (county-aware address lookup)
    8. Smarty ZIP recovery for AssuranceWeb counties (Madison/Marshall)
    9. Same-decedent + same-address dedupe (P0 #2, #3 per apn_probate)
    10. ZIP tier gate (Tier 1 + Tier 2)
    11. Tracerfy skip-trace + Trestle phone scoring
    12. write_datasift_csv → datasift_upload_probate_adhunter_<ts>.csv

Cross-source dedup with APN happens at DataSift upload time
(address-based). Within-run same-decedent + same-address dedup handles
overlap between our listing pages and detail-page-level correlations.

CLI:
    python src/adhunter_probate_pipeline.py --counties Madison,Marshall \\
        --tiers 1,2 --skip-trace \\
        --output-datasift-csv output/leads/datasift_upload_probate_adhunter.csv
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)


ADHUNTER_BASE = "https://classads.tnvalleymedia.com"
ADHUNTER_SEARCH = (
    f"{ADHUNTER_BASE}/AdHunter/Default/Home/Search"
    "?majorClass=10&minorClass=-1&perpage={perpage}&page={page}"
)
ADHUNTER_DETAIL = f"{ADHUNTER_BASE}/AdHunter/Default/Home/Ad/{{ad_id}}"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Cross-run dedup — same shape as .tb_results_seen_ids.json (c427ad3).
# AdHunter notices are visible for 15-21 days (3-week AL statutory publication
# window), so a 30-day prune keeps enough history to catch republished ads
# without ballooning the file.
_SEEN_IDS_PATH = Path(__file__).parent.parent / ".adhunter_probate_seen_ids.json"
_SEEN_PRUNE_DAYS = 30

# Title pre-filter — cheap; skips detail fetch for non-probate notices.
_TITLE_PROBATE_RE = re.compile(
    r"(IN\s+THE\s+PROBATE\s+COURT|IN\s+THE\s+MATTER\s+OF\s+THE\s+ESTATE|"
    r"NOTICE\s+TO\s+CREDITORS|LETTERS\s+(?:TESTAMENTARY|OF\s+ADMINISTRATION))",
    re.IGNORECASE,
)

# County detection from notice body — the same pattern apn_probate uses
# for is_target_county but scoped tighter (probate-specific).
_COUNTY_RE = re.compile(
    r"(?:Judge\s+of\s+Probate\s+Court|Probate\s+Court)\s+of\s+"
    r"(Madison|Marshall|Jefferson|Lawrence|Morgan|Franklin|Colbert|Lauderdale|"
    r"Limestone|Cullman|Blount|St\.?\s*Clair)\s+County",
    re.IGNORECASE,
)

# Decedent name — three patterns, applied in priority order.
_DECEDENT_PATTERNS = [
    re.compile(
        r"Letters\s+(?:Testamentary|of\s+Administration)\s+"
        r"(?:on\s+the\s+)?Estate\s+of\s+([A-Z][A-Za-z.\-'\s,]+?),?\s+deceased",
        re.IGNORECASE,
    ),
    re.compile(
        r"Estate\s+of\s+([A-Z][A-Za-z.\-'\s,]+?),?\s+deceased",
        re.IGNORECASE,
    ),
    re.compile(
        r"IN\s+THE\s+MATTER\s+OF\s+THE\s+ESTATE\s+OF\s+"
        r"([A-Z][A-Za-z.\-'\s,]+?),?\s+deceased",
        re.IGNORECASE,
    ),
]

# Personal representative — the person granted Letters. Multiple prose shapes.
_PR_PATTERNS = [
    re.compile(
        r"(?:having\s+been\s+)?granted\s+to\s+(?:the\s+undersigned,?\s+)?"
        r"([A-Z][A-Za-z.\-'\s,]+?)\s+(?:on|by|the|as)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"Letters\s+(?:Testamentary|of\s+Administration).*?"
        r"granted\s+to\s+([A-Z][A-Za-z.\-'\s,]+?)[,.]",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"Personal\s+Representative:?\s*([A-Z][A-Za-z.\-'\s,]+?)[,.\n]",
        re.IGNORECASE,
    ),
]

# Case number — AL probate courts use varied formats.
_CASE_PATTERNS = [
    re.compile(r"Case\s+No[.:]?\s*([A-Z0-9\-]+)", re.IGNORECASE),
    re.compile(r"Case\s+Number[.:]?\s*([A-Z0-9\-]+)", re.IGNORECASE),
    re.compile(r"\b(PR[A-Z]?-?20\d{2}-?\d{3,6})\b", re.IGNORECASE),
]

# Notice subtype detection — routes probate_sale + probate_final_settlement
# separately from the default probate_creditors so downstream filter presets
# can prioritize sale-petition notices (highest-signal distress — estate is
# actively selling the property).
_SUBTYPE_PATTERNS = [
    (re.compile(
        r"Petition\s+(?:for\s+)?Authority\s+to\s+Sell\s+Real\s+Property|"
        r"Petition\s+for\s+Sale\s+of\s+Real\s+(?:Estate|Property)",
        re.IGNORECASE,
    ), "probate_sale"),
    (re.compile(
        r"Petition\s+for\s+Final\s+Settlement", re.IGNORECASE,
    ), "probate_final_settlement"),
    (re.compile(
        r"NOTICE\s+TO\s+CREDITORS|Letters\s+(?:Testamentary|of\s+Administration)",
        re.IGNORECASE,
    ), "probate_creditors"),
]

# Hearing date — "on the 21 day of July, 2026, at 11:00 a.m."
_HEARING_DATE_RE = re.compile(
    r"(?:hearing|petition).{0,80}?(?:on|held).{0,20}?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# Granted date — "on the 8th day of July, 2026". The "granted" and the
# date-phrase can be up to ~250 chars apart in AL probate boilerplate
# (the intervening PR-role clause is often long). Non-greedy '.' with a
# larger cap; anchor on "day of MONTH, YEAR" which is unambiguous.
_GRANTED_DATE_RE = re.compile(
    r"granted.{0,250}?on\s+(?:the\s+)?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
    re.IGNORECASE | re.DOTALL,
)
# Standalone "on the Nth day of MONTH, YEAR" — fallback when the primary
# distance-bounded pattern misses.
_DATE_PHRASE_RE = re.compile(
    r"on\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
    re.IGNORECASE,
)

# Ad-listing regex — parse AdHunter <a href="/AdHunter/Default/Home/Ad/{id}"> URLs
_AD_LINK_RE = re.compile(r"/AdHunter/Default/Home/Ad/(\d+)")
# Publication domain — the teaser mentions e.g. "advertisergleam.com"
_PUB_DOMAIN_RE = re.compile(r"([a-z0-9\-]+\.(?:com|net))", re.IGNORECASE)
# Date range — "7/8/2026-7/22/2026" or single "7/8/2026"
_DATE_RANGE_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})(?:\s*-\s*(\d{1,2}/\d{1,2}/\d{4}))?"
)


# ── Publication → primary county mapping ──────────────────────────────

# Maps publication domain (as seen in AdHunter teasers) to the primary
# county its legal notices cover. Used to route papers we care about,
# not as a hard filter (a notice's body county reference is authoritative).
_PUB_TARGET = {
    "themadisonrecord.com": "Madison",
    "madisonrecord.com": "Madison",     # canonicalization variant
    "advertisergleam.com": "Marshall",
    "redstonerocket.com": "Madison",    # Redstone Arsenal / south-Huntsville
}


# ── Result schema ────────────────────────────────────────────────────


@dataclass
class AdHunterNotice:
    """One AdHunter classifieds ad (post-fetch, pre-enrichment)."""

    ad_id: str
    title: str = ""
    teaser: str = ""
    publication_domain: str = ""
    date_range_raw: str = ""
    start_date: str = ""    # ISO, first date in range
    end_date: str = ""      # ISO, last date in range (or same as start)
    detail_url: str = ""

    # Populated from detail page
    raw_text: str = ""
    county_of_notice: str = ""     # "Madison" | "Marshall" | "" (dropped)
    decedent_name: str = ""
    personal_rep_name: str = ""
    case_number: str = ""
    granted_date: str = ""         # ISO YYYY-MM-DD
    notice_subtype: str = ""       # probate_creditors | probate_sale | probate_final_settlement
    hearing_date: str = ""         # ISO — set for probate_sale/final_settlement


@dataclass
class AdHunterResult:
    """End-to-end outcome for one AdHunter probate notice."""

    notice: "NoticeData"
    ad_id: str
    county: str = ""
    property_found: bool = False
    tier: int | None = None
    status: str = "unknown"
    notes: str = ""


# ── Fetch: listing pages ─────────────────────────────────────────────


def _http_get(url: str, *, timeout: float = 30.0) -> str:
    """Simple GET with UA + retries on transient failures."""
    for attempt in range(3):
        try:
            resp = requests.get(
                url, headers={"User-Agent": _UA}, timeout=timeout,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if attempt == 2:
                raise
            logger.warning("HTTP GET %s failed (attempt %d): %s — retrying",
                           url, attempt + 1, e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_listing_page(page: int, *, perpage: int = 100) -> list[AdHunterNotice]:
    """Fetch one AdHunter search-results page and parse into notices.

    Returns [] when the page number is past the last real page (AdHunter
    silently returns an empty result list, no 404).
    """
    url = ADHUNTER_SEARCH.format(page=page, perpage=perpage)
    logger.debug("Fetching AdHunter listing page %d: %s", page, url)
    html = _http_get(url)
    return _parse_listing_page(html)


def _parse_listing_page(html: str) -> list[AdHunterNotice]:
    """Parse an AdHunter listing page.

    Ad shell structure (verified 2026-07-09):
        <div class="media-body">
          <h4 class="media-heading">
            <a href="/AdHunter/Default/Home/Ad/{id}">{TITLE}</a>
          </h4>
          {PUB_DOMAIN} {NOTICE_BODY_PREVIEW}... {DATE_RANGE} #{ID}
        </div>

    Title lives on the anchor; publication domain, teaser body, date range,
    and ad ID all sit alongside as sibling text nodes inside media-body.
    """
    soup = BeautifulSoup(html, "html.parser")
    notices: list[AdHunterNotice] = []
    seen_ids_this_page: set[str] = set()

    for body in soup.select("div.media-body"):
        heading = body.find(["h4", "h3", "h5"], class_="media-heading")
        if not heading:
            continue
        link = heading.find("a", href=True)
        if not link:
            continue
        m = _AD_LINK_RE.search(link.get("href") or "")
        if not m:
            continue
        ad_id = m.group(1)
        if ad_id in seen_ids_this_page:
            continue
        seen_ids_this_page.add(ad_id)

        title = link.get_text(" ", strip=True)

        # Full container text includes: title + pub domain + notice preview
        # + date range + "#{id}". We keep the pub-and-body portion as the
        # teaser for filtering + fallback extraction.
        full_text = body.get_text(" ", strip=True)
        # Strip the title (appears once at the start of the container text)
        teaser = full_text
        if title and teaser.startswith(title):
            teaser = teaser[len(title):].lstrip()

        pub_m = _PUB_DOMAIN_RE.search(teaser)
        pub = pub_m.group(1).lower() if pub_m else ""

        dr_m = _DATE_RANGE_RE.search(teaser)
        date_range_raw = ""
        start_iso = ""
        end_iso = ""
        if dr_m:
            date_range_raw = dr_m.group(0)
            start_iso = _mmddyyyy_to_iso(dr_m.group(1))
            end_iso = _mmddyyyy_to_iso(dr_m.group(2)) if dr_m.group(2) else start_iso

        notices.append(AdHunterNotice(
            ad_id=ad_id,
            title=title,
            teaser=teaser,
            publication_domain=pub,
            date_range_raw=date_range_raw,
            start_date=start_iso,
            end_date=end_iso,
            detail_url=ADHUNTER_BASE + link["href"],
        ))

    return notices


def _mmddyyyy_to_iso(mmddyyyy: str) -> str:
    try:
        return datetime.strptime(mmddyyyy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def fetch_all_listings(
    *, perpage: int = 100, max_pages: int = 30, delay_seconds: float = 0.5,
) -> list[AdHunterNotice]:
    """Paginate through all AdHunter public notices until an empty page."""
    all_notices: list[AdHunterNotice] = []
    for page in range(1, max_pages + 1):
        batch = fetch_listing_page(page, perpage=perpage)
        if not batch:
            logger.info("AdHunter listing exhausted at page %d", page)
            break
        all_notices.extend(batch)
        logger.info("AdHunter page %d: %d notices (running total: %d)",
                    page, len(batch), len(all_notices))
        time.sleep(delay_seconds)
    return all_notices


# ── Filter: title pre-check + publication routing ────────────────────


def title_looks_like_probate(title: str) -> bool:
    """Cheap pre-check that avoids fetching detail pages for non-probate ads."""
    return bool(_TITLE_PROBATE_RE.search(title or ""))


def publication_is_target(pub_domain: str) -> bool:
    """True if the publication maps to a target county (Madison/Marshall)."""
    return pub_domain.lower() in _PUB_TARGET


# ── Fetch: detail page + field extraction ────────────────────────────


def fetch_detail(notice: AdHunterNotice) -> AdHunterNotice:
    """Fetch the notice's detail page and populate raw_text + extracted fields.

    Idempotent: safe to call twice. On HTTP failure logs a warning and leaves
    raw_text empty; caller should treat as unparseable.
    """
    if notice.raw_text:
        return notice
    try:
        html = _http_get(notice.detail_url)
    except requests.RequestException as e:
        logger.warning("AdHunter detail fetch failed for ad %s: %s",
                       notice.ad_id, e)
        return notice

    soup = BeautifulSoup(html, "html.parser")
    # AdHunter detail pages wrap the actual notice in
    # <div class="item panel-body"> — verified 2026-07-09. Fall back to
    # <div class="panel panel-default"> (adds a small "Ad Text" prefix)
    # and finally to the whole page text as a last resort.
    body = (
        soup.select_one("div.item.panel-body")
        or soup.select_one("div.panel.panel-default")
    )
    text = body.get_text(" ", strip=True) if body else soup.get_text(" ", strip=True)
    notice.raw_text = text

    # Extract fields
    notice.county_of_notice = _extract_county(text)
    notice.decedent_name = _extract_decedent(text)
    notice.personal_rep_name = _extract_pr(text)
    notice.case_number = _extract_case_number(text)
    notice.granted_date = _extract_granted_date(text)
    notice.notice_subtype = _extract_subtype(text)
    if notice.notice_subtype in {"probate_sale", "probate_final_settlement"}:
        notice.hearing_date = _extract_hearing_date(text)

    return notice


def _extract_subtype(text: str) -> str:
    """First-match wins; defaults to probate_creditors if any Letters trigger."""
    for pat, subtype in _SUBTYPE_PATTERNS:
        if pat.search(text):
            return subtype
    return ""


def _extract_hearing_date(text: str) -> str:
    m = _HEARING_DATE_RE.search(text)
    if not m:
        return ""
    day, month_name, year = m.group(1), m.group(2), m.group(3)
    try:
        return datetime.strptime(
            f"{day} {month_name} {year}", "%d %B %Y",
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_county(text: str) -> str:
    m = _COUNTY_RE.search(text)
    if not m:
        return ""
    return m.group(1).strip().title()


def _extract_decedent(text: str) -> str:
    for pat in _DECEDENT_PATTERNS:
        m = pat.search(text)
        if m:
            return _clean_name(m.group(1))
    return ""


def _extract_pr(text: str) -> str:
    for pat in _PR_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate = _clean_name(m.group(1))
        # Guard: reject sub-5-char or lowercase-noise-word captures (e.g.
        # a run where the notice literally says "granted to the undersigned"
        # or omits the PR name entirely, and the regex grabbed a filler word).
        if len(candidate) < 5:
            continue
        if candidate.lower() in {"the undersigned", "undersigned", "on"}:
            continue
        return candidate
    return ""


def _extract_case_number(text: str) -> str:
    for pat in _CASE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip().upper()
    return ""


def _extract_granted_date(text: str) -> str:
    # Primary: "granted ... on the Nth day of MONTH, YEAR" pattern
    m = _GRANTED_DATE_RE.search(text)
    # Fallback: any standalone "on the Nth day of MONTH, YEAR" — usually
    # the granted-date phrase even without the "granted" trigger nearby.
    if not m:
        m = _DATE_PHRASE_RE.search(text)
    if not m:
        return ""
    day, month_name, year = m.group(1), m.group(2), m.group(3)
    try:
        return datetime.strptime(
            f"{day} {month_name} {year}", "%d %B %Y",
        ).strftime("%Y-%m-%d")
    except ValueError:
        try:
            return datetime.strptime(
                f"{day} {month_name} {year}", "%d %b %Y",
            ).strftime("%Y-%m-%d")
        except ValueError:
            return ""


def _clean_name(raw: str) -> str:
    """Strip whitespace + trailing commas + noise words from a captured name."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = s.rstrip(",. ")
    # Drop obvious boilerplate that occasionally lands inside the capture group
    s = re.sub(r"\s+(as\s+.+|on\s+the\s+.+|by\s+the\s+.+|the\s+undersigned).*$",
               "", s, flags=re.IGNORECASE)
    return s.strip()


# ── Cross-run dedup ──────────────────────────────────────────────────


def load_seen_ids() -> dict[str, str]:
    """Load persisted ad IDs {ad_id: iso_date_first_seen}.

    Prunes entries older than _SEEN_PRUNE_DAYS to keep the file bounded.
    """
    if not _SEEN_IDS_PATH.exists():
        return {}
    try:
        raw = json.loads(_SEEN_IDS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load %s (%s); starting fresh",
                       _SEEN_IDS_PATH.name, e)
        return {}

    cutoff = (date.today() - timedelta(days=_SEEN_PRUNE_DAYS)).isoformat()
    return {k: v for k, v in raw.items() if isinstance(v, str) and v >= cutoff}


def save_seen_ids(seen: dict[str, str]) -> None:
    _SEEN_IDS_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True))


# ── NoticeData conversion ────────────────────────────────────────────


def to_notice_data(notice: AdHunterNotice) -> "NoticeData":
    """Convert an AdHunter probate notice into standard SiftStack NoticeData.

    Populates the probate-specific fields (decedent_name, case_number,
    granted_date, notice_subtype) alongside the standard shell. Address
    fields stay empty at this stage — probate_property_locator will fill
    them by decedent+PR name lookup against the county's tax roll.
    """
    from notice_parser import NoticeData
    n = NoticeData()
    n.notice_type = "probate"
    n.notice_subtype = notice.notice_subtype or "probate_creditors"
    n.county = notice.county_of_notice or ""
    n.state = "AL"
    n.decedent_name = notice.decedent_name
    n.owner_name = notice.personal_rep_name    # PR name lives in owner_name slot
    n.case_number = notice.case_number
    n.granted_date = notice.granted_date
    n.hearing_date = notice.hearing_date
    n.date_added = notice.start_date or date.today().strftime("%Y-%m-%d")
    n.received_date = notice.start_date or n.date_added
    n.source_url = notice.detail_url
    n.raw_text = notice.raw_text[:8000] if notice.raw_text else ""
    return n


# ── Orchestrator ─────────────────────────────────────────────────────


def fetch_and_filter(
    *,
    counties: tuple[str, ...] = ("Madison", "Marshall"),
    seen_ids: dict[str, str] | None = None,
    perpage: int = 100,
    max_pages: int = 30,
) -> tuple[list[AdHunterNotice], dict[str, int]]:
    """Full listing sweep + filter to target-county probate notices.

    Returns (surviving_notices, funnel_counts). Funnel counts flow into
    the daily Slack summary via daily_finalize.

    Dedup: notices whose ``ad_id`` is already in ``seen_ids`` are skipped
    entirely — no detail fetch, no enrichment budget spent.
    """
    counties_lc = {c.lower() for c in counties}
    seen = seen_ids if seen_ids is not None else {}
    today_iso = date.today().isoformat()

    counts = {
        "fetched": 0,
        "title_probate": 0,
        "pub_target": 0,
        "already_seen": 0,
        "detail_fetched": 0,
        "county_match": 0,
        "kept": 0,
    }

    logger.info("AdHunter: fetching all listing pages (perpage=%d, max=%d)",
                perpage, max_pages)
    listings = fetch_all_listings(perpage=perpage, max_pages=max_pages)
    counts["fetched"] = len(listings)

    # Two-stage pre-filter avoids fetching detail pages we'll discard anyway
    prefiltered: list[AdHunterNotice] = []
    for n in listings:
        if not title_looks_like_probate(n.title):
            continue
        counts["title_probate"] += 1
        if not publication_is_target(n.publication_domain):
            continue
        counts["pub_target"] += 1
        if n.ad_id in seen:
            counts["already_seen"] += 1
            continue
        prefiltered.append(n)

    logger.info(
        "AdHunter pre-filter: %d fetched → %d probate title → %d target pub → "
        "%d after seen-dedup",
        counts["fetched"], counts["title_probate"], counts["pub_target"],
        len(prefiltered),
    )

    # Fetch detail pages + extract fields
    survivors: list[AdHunterNotice] = []
    for n in prefiltered:
        fetch_detail(n)
        counts["detail_fetched"] += 1
        # Body-county authoritative; publication mapping only steered us here
        body_county_lc = (n.county_of_notice or "").lower()
        if body_county_lc not in counties_lc:
            logger.debug("  DROP %s: county %r not in target set",
                         n.ad_id, n.county_of_notice)
            continue
        counts["county_match"] += 1

        # Sanity gate: notices without any extracted decedent get dropped —
        # they're either mis-classified public notices (subdivision boundary
        # notices, minute-order hearings) or the extractor missed. Log at INFO
        # so we can spot patterns to add.
        if not n.decedent_name:
            logger.info("  DROP %s (no decedent): title=%r",
                        n.ad_id, n.title[:80])
            continue

        survivors.append(n)
        counts["kept"] += 1
        seen[n.ad_id] = today_iso    # mark as seen only after enrichment sanity

        time.sleep(0.3)              # polite delay between detail fetches

    logger.info(
        "AdHunter detail pass: %d probed → %d county-match → %d kept",
        counts["detail_fetched"], counts["county_match"], counts["kept"],
    )

    return survivors, counts


def enrich_and_gate(
    notices: list[AdHunterNotice],
    *,
    counties: tuple[str, ...] = ("Madison", "Marshall"),
    tier_filter: tuple[int, ...] | None = (1, 2),
) -> tuple[list[AdHunterResult], dict[str, int]]:
    """Route surviving notices through probate_property_locator + Smarty + tier gate.

    Mirrors apn_probate_pipeline_al._pipeline_run's stages 2-4 (P0 dedup
    against decedent + address, ZIP tier filter). Skip-trace + Trestle run
    downstream in _main() so this function stays synchronous + testable.
    """
    from probate_property_locator import enrich_notice_with_property
    from address_standardizer import (
        smarty_zip_or_city_estimate_for_madison,
        smarty_zip_or_city_estimate_for_marshall,
    )
    from target_zips import zip_tier_county

    counts = {
        "input": len(notices),
        "duplicate_decedent": 0,
        "no_property": 0,
        "duplicate_property": 0,
        "off_tier": 0,
        "enriched": 0,
    }

    seen_decedents: set[str] = set()
    seen_addresses: set[tuple[str, str]] = set()
    results: list[AdHunterResult] = []

    for n in notices:
        nd = to_notice_data(n)
        result = AdHunterResult(notice=nd, ad_id=n.ad_id, county=nd.county)

        # Same-decedent dedupe (P0 #2 from apn_probate).
        dec_key = _normalize_decedent_key(
            nd.decedent_name or "", nd.granted_date or "",
        )
        if dec_key and dec_key in seen_decedents:
            result.status = "dropped_duplicate_decedent"
            result.notes = f"duplicate decedent (key={dec_key})"
            counts["duplicate_decedent"] += 1
            results.append(result)
            continue
        if dec_key:
            seen_decedents.add(dec_key)

        # Stage 2: probate_property_locator (name-based address lookup)
        try:
            matched = enrich_notice_with_property(nd)
        except Exception as e:
            logger.warning("Property locator failed for %s: %s",
                           nd.decedent_name, e)
            result.status = "error"
            result.notes = f"locator error: {e}"
            results.append(result)
            continue

        if not matched or not nd.address:
            result.status = "dropped_no_property"
            counts["no_property"] += 1
            results.append(result)
            continue

        result.property_found = True

        # Stage 3: Smarty ZIP recovery for AssuranceWeb (Madison/Marshall)
        county_lc = (nd.county or "").lower()
        if not nd.zip and nd.address and county_lc in {"madison", "marshall"}:
            try:
                if county_lc == "marshall":
                    city, zip5, est = smarty_zip_or_city_estimate_for_marshall(
                        nd.address,
                    )
                else:
                    city, zip5, est = smarty_zip_or_city_estimate_for_madison(
                        nd.address,
                    )
                if zip5:
                    nd.zip = zip5
                    if not nd.city and city:
                        nd.city = city
                    if est:
                        existing = nd.missing_data_flags or ""
                        nd.missing_data_flags = (
                            f"{existing}|zip_estimated_from_city" if existing
                            else "zip_estimated_from_city"
                        )
            except Exception as e:
                logger.debug("Smarty ZIP recovery failed for %s: %s",
                             nd.address, e)

        # Same-property dedupe (P0 #3)
        addr_key = (
            (nd.address or "").strip().upper(),
            (nd.zip or "").strip()[:5],
        )
        if addr_key[0] and addr_key in seen_addresses:
            result.status = "dropped_duplicate_property"
            counts["duplicate_property"] += 1
            results.append(result)
            continue
        if addr_key[0]:
            seen_addresses.add(addr_key)

        # Stage 4: ZIP tier gate
        tier, _ = zip_tier_county(nd.zip)
        result.tier = tier
        if tier_filter and (tier is None or tier not in tier_filter):
            result.status = "dropped_off_target"
            counts["off_tier"] += 1
            results.append(result)
            continue

        result.status = "enriched"
        counts["enriched"] += 1
        results.append(result)

    logger.info(
        "AdHunter enrichment: in=%d dup_dec=%d no_prop=%d dup_prop=%d "
        "off_tier=%d KEPT=%d",
        counts["input"], counts["duplicate_decedent"], counts["no_property"],
        counts["duplicate_property"], counts["off_tier"], counts["enriched"],
    )
    return results, counts


def _normalize_decedent_key(name: str, granted_date: str) -> str:
    """Same normalization apn_probate uses for cross-run dedup."""
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    d = (granted_date or "").strip()
    return f"{n}|{d}" if n else ""


# ── CLI ──────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adhunter_probate_pipeline",
        description=(
            "TN Valley Media AdHunter probate adapter — Madison + Marshall "
            "County estates. Closes the APN publication gap for Madison and "
            "complements APN coverage for Marshall."
        ),
    )
    p.add_argument(
        "--counties", default="Madison,Marshall",
        help="Comma-separated counties (default: Madison,Marshall).",
    )
    p.add_argument(
        "--tiers", default="1,2",
        help="Comma-separated ZIP tiers (default '1,2', 'all' disables).",
    )
    p.add_argument(
        "--skip-trace", action="store_true",
        help="Run Tracerfy batch skip-trace + Trestle phone scoring.",
    )
    p.add_argument(
        "--max-pages", type=int, default=30,
        help="Cap on listing pages to fetch (default 30, ~3000 notices).",
    )
    p.add_argument(
        "--perpage", type=int, default=100,
        help="Rows per listing page (default 100, AdHunter max).",
    )
    p.add_argument(
        "--output-datasift-csv", type=Path, default=None,
        help="Path to write DataSift-formatted CSV.",
    )
    p.add_argument(
        "--no-seen-dedup", action="store_true",
        help="Ignore .adhunter_probate_seen_ids.json — re-emit all ads.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
    )
    return p


def _main(argv: list[str]) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path.home() / "Desktop/SiftStack/.env")

    counties = tuple(c.strip() for c in args.counties.split(",") if c.strip())
    tier_filter = None
    if args.tiers.lower() != "all":
        tier_filter = tuple(int(t) for t in args.tiers.split(",") if t.strip())

    # Load persistent seen IDs (unless --no-seen-dedup)
    seen_ids = {} if args.no_seen_dedup else load_seen_ids()
    logger.info("Loaded %d seen ad IDs from %s",
                len(seen_ids), _SEEN_IDS_PATH.name)

    # Phase 1: listing + title-filter + pub-filter + detail-extract
    survivors, prefunnel = fetch_and_filter(
        counties=counties,
        seen_ids=seen_ids,
        perpage=args.perpage,
        max_pages=args.max_pages,
    )

    if not survivors:
        logger.info("AdHunter: 0 target-county probate notices this run.")
        if not args.no_seen_dedup:
            save_seen_ids(seen_ids)
        return 1

    # Phase 2: enrichment + tier gate
    results, enrfunnel = enrich_and_gate(
        survivors,
        counties=counties,
        tier_filter=tier_filter,
    )
    enriched = [r for r in results if r.status == "enriched"]
    notices = [r.notice for r in enriched]
    if not notices:
        logger.info("AdHunter: 0 records survived enrichment + tier gate.")
        if not args.no_seen_dedup:
            save_seen_ids(seen_ids)
        return 1

    # Phase 3: Smarty USPS standardization (parity with the other adapters)
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
            logger.info("Smarty standardization: %d/%d DPV-confirmed",
                        confirmed, len(notices))
        else:
            logger.info("Smarty standardization skipped — no credentials.")
    except Exception as e:
        logger.warning("Smarty standardization failed (continuing): %s", e)

    # Phase 4: Tracerfy + Trestle
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
                logger.warning("Skip-trace failed (continuing): %s", e)
            try:
                from phone_validator import score_phones_for_pipeline
                phone_tiers = score_phones_for_pipeline(notices)
            except Exception as e:
                logger.warning("Trestle scoring failed (continuing): %s", e)

    # Phase 5: DataSift CSV write
    if args.output_datasift_csv:
        from datasift_formatter import write_datasift_csv
        path = write_datasift_csv(
            notices, str(args.output_datasift_csv),
            phone_tiers=phone_tiers,
        )
        print(f"Wrote DataSift CSV: {path}")

    # Emit funnel line for daily_finalize parser (same shape as
    # other pipelines' log lines)
    total_funnel = {**prefunnel, **enrfunnel}
    logger.info("Funnel (adhunter_probate): %s", total_funnel)

    # Persist seen IDs (only if we ran a real dedup pass)
    if not args.no_seen_dedup:
        save_seen_ids(seen_ids)
        logger.info("Persisted %d seen ad IDs → %s",
                    len(seen_ids), _SEEN_IDS_PATH.name)

    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
