"""Jefferson County, AL pre-probate pipeline orchestrator.

Companion to ``benchmark_pipeline_al.py``. Where post-probate pulls cases
from court records (Benchmark Web), pre-probate pulls fresh obituaries
(legacy.com Birmingham aggregator) and works backwards: obituary →
decedent name → Jefferson property API → Tier 1/Tier 2 ZIP gate →
enrichment → DataSift CSV + Slack.

Pipeline stages:

    1. Harvest obituary URLs from the Birmingham listing
    2. For each obit URL: fetch text + LLM-extract decedent + survivors
    3. Search Jefferson property API by decedent name
    4. Pick primary parcel (homestead > highest-value)
    5. ZIP gate: keep only parcels in Tier 1 ∪ Tier 2
    6. For surviving cases: convert to NoticeData, run skip-trace, write CSV
    7. Slack notification

Reuses ~80% of the post-probate codebase: target_zips,
probate_property_locator, obituary_enricher (rank_decision_makers,
_fetch_page_text, _search_obituary's helpers), tracerfy_skip_tracer,
datasift_formatter. The genuinely new pieces are this module + the
obituary harvester + a per-obit decedent-extraction LLM prompt.

Time signature: pre-probate leads are days fresh (vs 30-90 days for
post-probate). Lower-confidence contact identification (no court-appointed
PR yet) but first-to-market timing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import datasift_formatter
import llm_client
from benchmark_obituary_match import (
    LLM_MAX_TOKENS,
    MAX_OBITUARY_TEXT,
    _parse_flexible_date,
)
from benchmark_pipeline_al import _promote_heir_contacts_to_csv_slots
from notice_parser import NoticeData, _split_decedent_name, _split_owner_name
from observability import (
    FunnelCounter,
    ServiceRateTracker,
    load_rolling_rates,
    rolling_rates_summary,
    save_rolling_rates,
)
from obituary_enricher import (
    SYSTEM_PROMPT,
    _fetch_page_text,
    rank_decision_makers,
)
from obituary_harvester import (
    ALABAMA_LISTINGS,
    HarvestedObit,
    harvest_alabama,
    harvest_birmingham,  # noqa: F401 — kept for backwards compat
)
from probate_property_locator import (
    _score,
    _search_jefferson,
    _search_madison,
    _search_marshall,
)
from slack_notifier import (
    _send_blocks_webhook,
    build_funnel_block,
    build_service_rates_block,
)
from target_zips import zip_tier_county

load_dotenv()

logger = logging.getLogger(__name__)


# Legacy.com person pages render the full obit body as a ~200-char preview
# plus a "Read the full obituary on our trusted partner sites below" panel
# that links out to obits.al.com OR a funeral-home partner site (Welch
# Funeral, Eastside, JM Gardens, Larkin & Scott, Messmer Goodwin,
# DignityMemorial, Arrington, Valhalla, etc.). The partner URLs are in the
# RAW HTML's <a href> attributes, but `_fetch_page_text` (which uses
# Firecrawl by default) strips these when rendering to markdown — so we
# do a separate raw HTTP GET on the legacy.com page just to extract the
# partner-URL list, then follow them in priority order.
import re as _re
import requests as _requests

# Domains we KNOW are obit/funeral sources and want to follow when we see
# them linked from a legacy.com preview. Higher priority = tried first.
# legacy.com/us/obituaries/ tops the list because: the partner panel on
# every modern legacy.com/person/ preview links to the matching FULL-obit
# listing at the same domain — this is where Clara Geneva Butler's 11
# survivors (and most other "0-heir" records) actually live. Before
# 2026-06-20 our regex explicitly EXCLUDED www.legacy.com URLs, so every
# legacy-to-legacy upgrade silently failed.
_PARTNER_DOMAIN_PRIORITY = [
    "legacy.com/us/obituaries",  # Same-site obit listing — has full text + survivor list
    "obits.al.com",          # AL.com syndication — full obit + survivor list
    "dignitymemorial.com",   # Major funeral chain; KNOWN_403, routes via Firecrawl
    "jmgardens.com",
    "messmergoodwin.com",
    "larkinandscott.com",
    "welchfh.com",
    "eastsidefuneralhome.net",
    "arringtonfuneralhome.com",
    "valhallafuneralhome.com",
    "obituaries.valhallafuneralhome.com",
    # AL funeral homes added 2026-06-13 — operator reported records
    # sourced from these sites had missing survivor data. Also added to
    # KNOWN_403_DOMAINS in obituary_enricher.py so they route via
    # Firecrawl (Etowah returns HTTP 403 to direct fetches).
    "etowahmemorialchapel.com",
]

# Patterns to SKIP (CDN, social, analytics, ads).
# memoriams.com is a "place an obituary anywhere" advertising service legacy
# links to as cross-promotion — its pages contain marketing copy that's
# 6000+ chars (full length) but ZERO obit content. Always skip.
# sympathy.legacy.com is flowers/gifts ad pages — never has obit content.
# cdn.legacy.com / static.legacy.com are CSS/JS asset hosts.
_PARTNER_SKIP_KEYWORDS = (
    "legacy.net", "memoriams.com", "facebook.com", "twitter.com",
    "googleapis", "tracking", "cloudfront", "amazonaws", "doubleclick",
    "media.legacy", "cache.legacy", "sympathy.legacy", "cdn.legacy",
    "static.legacy", "fundingchoices", "pub.network", "doubleclick",
    "googletagmanager", "btloader", "confiant", "youtube.com",
    "instagram.com", "tiktok.com", "pinterest.com",
)

# Phrases that indicate the fetched text is a REAL obituary, not navigation
# chrome or an ad page. We require at least one match before accepting a
# partner-site fetch. Expanded 2026-06-20 to cover regional/religious
# funeral-page phrasings that don't use the legalistic "survived by" wording
# (common on smaller AL funeral homes and DignityMemorial chapter pages).
_OBIT_MARKERS = (
    # Standard obit headers
    "survived by", "preceded in death", "predeceased", "leaves behind",
    # Death phrasings — secular
    "passed away", "passed peacefully", "passed quietly", "passed from",
    "passed into", "went to be with", "departed this life",
    "entered into rest", "entered into eternal rest",
    # Death phrasings — religious
    "went home to be with", "called home", "received her heavenly",
    "received his heavenly", "joined her Lord", "joined his Lord",
    "joined the Lord", "in the presence of", "to be with the Lord",
    # Biographical
    "born on", "born in", "was born", "the daughter of", "the son of",
    "the wife of", "the husband of",
    # Service / arrangement language (often present even without survivor list)
    "celebration of life", "funeral service", "memorial service",
    "graveside service", "visitation will be", "in lieu of flowers",
    "interment", "burial will", "officiating",
    # Survivors plural keyword used by some smaller funeral homes
    "she leaves to cherish", "he leaves to cherish",
    "loving memory of",
)


def _looks_like_obituary(text: str, min_chars: int = 500) -> bool:
    """Heuristic: does this text plausibly contain an obituary body?

    Requires both substantial length AND at least one of the obit-marker
    phrases. Rejects 6000-char ad pages (memoriams.com), short metadata-only
    pages (just the page title), and navigation chrome.
    """
    if not text or len(text) < min_chars:
        return False
    text_lc = text.lower()
    return any(marker in text_lc for marker in _OBIT_MARKERS)


_NAME_PARTS_FROM_SLUG_RE = _re.compile(
    r"/person/([A-Za-z][A-Za-z\-]+?)-(\d+)/?",
    _re.IGNORECASE,
)


def _decedent_slug_tokens(legacy_person_url: str) -> set[str]:
    """Extract lowercase name tokens from a legacy.com/person/* URL slug.

    Example: "/person/Clara-Geneva-Butler-61553980" → {"clara","geneva","butler"}

    Used to filter out wrong-person URLs from legacy.com's "more
    obituaries you might like" sidebar — without this filter the
    refresh could pull e.g. `name/emma-claros-obituary` for a James
    Ronald McFarland record (sidebar suggestion), passing all the
    real-obituary heuristics but for the wrong decedent.

    Returns empty set when the URL doesn't match the expected pattern
    (lets the caller skip the name-match filter and accept all).
    """
    m = _NAME_PARTS_FROM_SLUG_RE.search(legacy_person_url)
    if not m:
        return set()
    name_part = m.group(1)
    return {tok.lower() for tok in name_part.split("-") if len(tok) > 2}


def _decedent_surname(legacy_person_url: str) -> str:
    """Extract the lowercase surname (last name token) from a /person/ URL.

    Used for in-text decedent verification — surnames are far more
    discriminating than first names. "Gregory" or "James" appear on
    almost every obit page; "Segrest" or "McFarland" don't.

    Example: "/person/Clara-Geneva-Butler-61553980" → "butler"
    Returns empty string when the URL doesn't match the pattern OR
    the last token is too short (≤ 3 chars).
    """
    m = _NAME_PARTS_FROM_SLUG_RE.search(legacy_person_url)
    if not m:
        return ""
    tokens = [t.lower() for t in m.group(1).split("-") if len(t) > 3]
    return tokens[-1] if tokens else ""


def _extract_curated_partner_urls(html: str) -> list[str]:
    """Extract partner URLs from legacy.com's curated "Obituary partner
    sites" region — in DOCUMENT ORDER (= visual left-to-right).

    Operator instruction 2026-06-20: the curated region (visible to the
    user as boxes labeled "Carr Funeral Home", "Guntersville Memorial
    Chapel & Crematory", etc.) ALWAYS lists the most-complete obit
    source first. The leftmost box typically links to the funeral home
    that actually handled services — their syndicated obit at
    legacy.com/us/obituaries/... has the full text + survivor list.

    Document order matters: legacy.com renders the partner boxes
    left-to-right in the same order they appear in HTML, so the first
    `<a href>` inside the region is the leftmost / primary partner.
    """
    # Find the region by its aria-label. Grab everything up to the
    # closing </div> that matches the region's opening tag. The exact
    # nesting depth varies, so we use a loose terminator: the partner
    # region is always followed by either ANOTHER section, a closing
    # main wrapper, or end-of-body — grabbing up to the next
    # role="region" or 200 chars past the last </a> in the block is
    # safest.
    m = _re.search(
        r'aria-label="Obituary partner sites".*?(?=role="region"|</main>|</body>|$)',
        html, _re.DOTALL | _re.IGNORECASE,
    )
    if not m:
        return []
    region = m.group(0)
    # Extract hrefs in document order (left → right in the rendered grid).
    hrefs = _re.findall(r'<a\s+href="(https?://[^"]+)"', region)
    # Dedup while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for h in hrefs:
        if h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


def _extract_partner_urls_via_playwright(
    legacy_person_url: str, timeout_ms: int = 15000,
) -> list[str]:
    """Use a real headless browser to read legacy.com's partner URLs that
    only appear after JS hydration.

    Background (2026-06-20): legacy.com's "Obituary partner sites"
    box is empty in the server-rendered HTML for many records — the
    funeral home links (Patterson-Forest Grove for Mark Jerome Green,
    etc.) are fetched from a separate API by client-side JavaScript
    AFTER the initial render. Raw `requests` and Firecrawl's normal
    render both return the empty box. Only a real Chromium browser
    waiting on the hydration call sees the populated grid.

    Used as a FALLBACK when _extract_curated_partner_urls() (raw HTML)
    returns nothing. Browser launch is ~1-2s + a configurable wait
    for the partner box to populate — acceptable for the ~30% of
    records where it's needed.

    Returns URLs in document order (= visual left-to-right). Empty
    list when Playwright isn't installed, the page errors, or the
    partner box truly is empty.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.debug("Playwright not installed — skipping browser fallback")
        return []

    urls: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                page = context.new_page()
                # domcontentloaded is enough — we don't need every ad pixel
                # to finish, just the React tree to mount.
                page.goto(legacy_person_url,
                          wait_until="domcontentloaded", timeout=timeout_ms)
                # Wait specifically for the partner box to have at least one
                # anchor. Short timeout because many pages legitimately have
                # no partner sites (Mark/James/Gregory pattern), and we
                # don't want to block 15s waiting for content that won't
                # appear. 6s is long enough for the API call to complete.
                try:
                    page.wait_for_selector(
                        '[aria-label="Obituary partner sites"] a',
                        timeout=6000,
                    )
                except PwTimeout:
                    # No partner anchors appeared — page genuinely has none
                    pass
                # Extract hrefs in document order (= visual left-to-right)
                urls = page.evaluate("""() => {
                    const region = document.querySelector(
                        '[aria-label="Obituary partner sites"]'
                    );
                    if (!region) return [];
                    return Array.from(region.querySelectorAll('a[href]'))
                        .map(a => a.href)
                        .filter(h => h.startsWith('http'));
                }""")
            finally:
                browser.close()
    except Exception as e:
        logger.debug("Playwright partner fetch failed for %s: %s",
                     legacy_person_url, e)
        return []

    # Dedup while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def _extract_partner_urls(legacy_person_url: str) -> list[str]:
    """Pull the raw legacy.com HTML and extract candidate partner-site URLs.

    Two-source extraction:
      1. CURATED partner region — legacy.com's "Obituary partner sites"
         box, in document order. Leftmost = primary partner (typically
         the actual funeral home that handled services). These are
         the highest-priority candidates and tried first.
      2. WHOLE-PAGE href scan — fallback when the curated region is
         empty or absent (older legacy.com layouts, partner-less
         pages). Filtered + priority-sorted by domain.

    The two lists are concatenated with curated URLs first, then
    duplicates removed (preserving order). This guarantees the
    curated picks win whenever legacy.com provides them.

    Filters out wrong-person sidebar suggestions: when the source URL
    slug yields a decedent name (e.g. "Clara-Geneva-Butler"), partner
    URLs whose slug doesn't share at least the last-name token are
    rejected. legacy.com aggressively cross-links unrelated obits in a
    "More Obituaries" sidebar; without this filter we'd happily upgrade
    James Ronald McFarland → "emma-claros-obituary" because both pass
    the keyword + obit-marker validation.
    """
    try:
        r = _requests.get(
            legacy_person_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=15, allow_redirects=True,
        )
    except Exception as e:
        logger.debug("Partner-URL fetch failed for %s: %s", legacy_person_url, e)
        return []
    if r.status_code != 200:
        return []

    # CURATED region first — these are the user-visible "partner sites"
    # boxes, in document order (= left-to-right). Highest priority.
    curated = _extract_curated_partner_urls(r.text)
    if curated:
        logger.debug(
            "Found %d curated partner URL(s) in 'Obituary partner sites' region (raw HTML)",
            len(curated),
        )
    else:
        # Raw HTML's curated region was empty — the partner box exists
        # but legacy.com hydrates the funeral home links via a client-
        # side API call AFTER initial render. Fall back to a real
        # browser via Playwright. ~5s overhead per call but recovers
        # records like Mark Jerome Green where the Patterson-Forest
        # Grove link is JS-only.
        logger.debug(
            "Raw HTML partner region empty — trying Playwright JS-render fallback"
        )
        curated = _extract_partner_urls_via_playwright(legacy_person_url)
        if curated:
            logger.info(
                "Playwright recovered %d partner URL(s) from JS-hydrated region",
                len(curated),
            )

    # All hrefs — we used to exclude every legacy.com URL because the
    # original intent was "follow only EXTERNAL partner sites". That
    # broke the most common upgrade path: legacy.com/person/* (preview)
    # links to legacy.com/us/obituaries/name/* (full obit listing on the
    # same domain — Clara Geneva Butler's 11 survivors were here all
    # along, 2026-06-20). Now we include same-site URLs and rely on:
    #   (1) the URL != legacy_person_url skip below (no self-loops)
    #   (2) _PARTNER_SKIP_KEYWORDS (sympathy.legacy, cdn.legacy, ads)
    #   (3) the obit-related keyword filter
    # to weed out the unhelpful legacy.com URLs (preview pages, sympathy
    # flower ads, asset CDN, etc.) while keeping the listing pages.
    hrefs = _re.findall(r'href=["\'](https?://[^"\']+)["\']', r.text)
    fallback_candidates: list[str] = []
    seen: set[str] = set()
    legacy_url_lower = legacy_person_url.lower()
    decedent_tokens = _decedent_slug_tokens(legacy_person_url)

    # Curated URLs already in `curated` (from _extract_curated_partner_urls)
    # are processed FIRST below — they preserve document order (leftmost
    # = primary funeral home) and bypass the priority sort. We still need
    # to apply the wrong-person filter to them as a safety check.
    curated_validated: list[str] = []
    for h in curated:
        if h in seen:
            continue
        seen.add(h)
        h_lower = h.lower()
        if h_lower == legacy_url_lower:
            continue
        if "/person/" in h_lower:
            continue
        # Wrong-person filter (same as the main loop below)
        if decedent_tokens:
            slug_match = None
            for pattern in (
                r"/name/([a-z][a-z\-]+?)(?:-obituary|/|\?|$)",
                r"/tribute/([a-z][a-z\-]+?)(?:/|\?|$)",
                r"/obituaries/[a-z\-]+/([a-z][a-z\-]+?)-\d",
            ):
                slug_match = _re.search(pattern, h_lower)
                if slug_match:
                    break
            if slug_match:
                slug_tokens = {t for t in slug_match.group(1).split("-") if len(t) > 2}
                if not (slug_tokens & decedent_tokens):
                    logger.debug(
                        "Curated partner rejected (wrong decedent slug): %s",
                        h,
                    )
                    continue
        curated_validated.append(h)

    for h in hrefs:
        if h in seen:
            continue
        seen.add(h)
        h_lower = h.lower()
        # No self-loops — don't follow the URL we just fetched
        if h_lower == legacy_url_lower:
            continue
        # No other /person/ preview pages — they're the same data we already have
        if "/person/" in h_lower:
            continue
        if any(skip in h_lower for skip in _PARTNER_SKIP_KEYWORDS):
            continue
        # Skip listing / FAQ / advice / charity / topic pages — they
        # contain obit-like text (lots of "survived by" etc.) but for
        # OTHER decedents or generic content. Without this filter,
        # legacy.com pages like /us/obituaries/local/north-carolina/durham
        # pass the obit-marker check (they list real Durham obits) and
        # become the "effective URL" for an AL decedent. Hard-reject.
        if any(seg in h_lower for seg in (
            "/local/", "/category/", "/advice/", "/charity/",
            "/groups/", "/memorial-writing/", "/contact/",
            "/funeral-homes/",          # listing of funeral homes, not an obit
            "/articles/", "/blog/",
        )):
            continue
        # Skip the root listing pages (no slug after /us/obituaries)
        if h_lower.rstrip("/").endswith("/us/obituaries"):
            continue
        # Must look obit-related (the URL slug or path mentions it)
        if not any(k in h_lower for k in (
            "obituar", "memorial", "funeral", "tribute", "death",
            "in-memory", "tribute-archive",
        )):
            continue

        # Wrong-decedent filter: legacy.com aggressively cross-links
        # OTHER decedents' obit pages in a "More Obituaries" sidebar on
        # every /person/ preview. Without this filter we'd happily
        # upgrade James Ronald McFarland → `name/emma-claros-obituary`
        # because emma-claros's page passes every other validity check.
        #
        # Three URL patterns expose the slug we can name-check against:
        #   legacy.com:           /name/{first-last}-obituary?id=...
        #   dignitymemorial.com:  /obituaries/{city-state}/{first-last}-{id}
        #   tribute-archive:      /tribute/{first-last}/...
        # Funeral home roots / regional listings / FAQ pages skip this
        # check (no decedent slug present, so name-mismatch is moot).
        if decedent_tokens:
            slug_match = None
            for pattern in (
                r"/name/([a-z][a-z\-]+?)(?:-obituary|/|\?|$)",        # legacy
                r"/tribute/([a-z][a-z\-]+?)(?:/|\?|$)",                # tribute-archive
                r"/obituaries/[a-z\-]+/([a-z][a-z\-]+?)-\d",           # dignitymemorial /obituaries/{city}/{first-last}-{id}
            ):
                slug_match = _re.search(pattern, h_lower)
                if slug_match:
                    break
            if slug_match:
                slug_tokens = {t for t in slug_match.group(1).split("-") if len(t) > 2}
                # Require at least one shared name token (typically the
                # last name — first names are sometimes nicknamed e.g.
                # "Jenny" vs "Clara Geneva", so last-name overlap is
                # the strongest signal).
                if not (slug_tokens & decedent_tokens):
                    continue

        fallback_candidates.append(h)

    # Sort the fallback (regex-scanned) candidates by domain + path priority.
    # Then return curated FIRST (document order preserved) + fallback after.
    # Final order: [curated leftmost, curated next, ..., best-priority fallback, ...]
    def _priority(u: str) -> tuple[int, int, str]:
        u_lower = u.lower()
        domain_rank = len(_PARTNER_DOMAIN_PRIORITY)
        for i, dom in enumerate(_PARTNER_DOMAIN_PRIORITY):
            if dom in u_lower:
                domain_rank = i
                break
        # Secondary rank: individual-obit URL patterns beat listing pages.
        # 0 = individual obit (/name/, /obituary?id=, /tribute/)
        # 1 = catch-all
        # 2 = listing pages (/local/, /category/, /advice/, /us/obituaries/ root)
        if "/name/" in u_lower or "obituary?id=" in u_lower or "/tribute/" in u_lower:
            path_rank = 0
        elif any(seg in u_lower for seg in ("/local/", "/category/", "/advice/",
                                              "/local-obituaries/", "/regional/")):
            path_rank = 2
        else:
            path_rank = 1
        return (domain_rank, path_rank, u_lower)

    fallback_candidates.sort(key=_priority)
    return curated_validated + fallback_candidates


def _fetch_full_obit_text(url: str) -> tuple[str, str]:
    """Fetch the most complete obit text we can find for ``url``.

    Returns (text, effective_url). When ``url`` is a legacy.com person page
    (which only shows a preview snippet), extract partner URLs from the raw
    HTML and try them in priority order (obits.al.com first, then funeral
    home sites). Validate each candidate with ``_looks_like_obituary``
    (length + obit-marker phrases) before accepting — this rejects ad pages
    that return 6000 chars of marketing copy but zero obit content.

    If no partner page validates, falls back to the original legacy.com
    preview text (still usable for decedent ID + DOD via the relaxed prompt).
    """
    text = _fetch_page_text(url)

    if "legacy.com/person/" in url:
        partner_urls = _extract_partner_urls(url)
        if partner_urls:
            logger.debug("Legacy person-page: %d partner URL(s) found", len(partner_urls))
        decedent_tokens = _decedent_slug_tokens(url)
        # Bumped to 6 attempts (was 4): the same-site legacy.com upgrade
        # path 2026-06-20 can include 2-3 regional/listing pages BEFORE
        # the actual obit listing, and the secondary path_rank sort fixes
        # ordering but doesn't guarantee position 1 (some funeral home
        # URLs may share priority slot). 6 attempts at ~3s each = 18s
        # worst-case per record, acceptable for a per-day pipeline.
        for partner_url in partner_urls[:6]:
            partner_text = _fetch_page_text(partner_url)
            if not _looks_like_obituary(partner_text, min_chars=500):
                logger.debug(
                    "Partner-page rejected (len=%d, no obit markers): %s",
                    len(partner_text) if partner_text else 0, partner_url,
                )
                continue
            # In-text decedent-name check — last line of defense against
            # a page that has obit markers but is about somebody else
            # (the generic /funeral-homes lobby page, a regional listing
            # that slipped through, etc.). Require the SURNAME (last
            # slug token) to appear in the page text. Surnames like
            # "Segrest" or "McFarland" are discriminating; first names
            # like "James" or "Gregory" appear on too many unrelated
            # obit pages to be useful filters.
            decedent_surname = _decedent_surname(url)
            if decedent_surname and decedent_surname not in partner_text.lower():
                logger.debug(
                    "Partner-page rejected (decedent surname '%s' absent): %s",
                    decedent_surname, partner_url,
                )
                continue
            logger.debug("Partner-page upgrade: %s → %s (%d chars, obit-marker matched)",
                         url[-50:], partner_url[-60:], len(partner_text))
            return (partner_text, partner_url)

    return (text or "", url)


# ── Pre-probate LLM prompt ───────────────────────────────────────────


# Different from post-probate's SURVIVOR_PROMPT — no court-named petitioner
# to match against. Just extract decedent + family graph from the obituary.
DECEDENT_PROMPT = """\
Below is text from a candidate obituary page. The text may be a full \
obituary OR a preview snippet (legacy.com syndication often only shows \
the first paragraph and links the full text to a funeral-home partner \
site we can't see). Either is acceptable as long as the page identifies \
ONE specific deceased person — extract whatever you can.

Return a JSON object with these exact keys:

— Identification —
- "is_obituary": true if this page identifies ONE specific deceased \
person (even if only their name, age, date of death, and a brief bio are \
shown — full survivor list is NOT required). false ONLY if this is a \
multi-decedent listing page, navigation chrome, or unrelated content.
- "decedent_full_name": full name as printed in the obituary
- "decedent_first_name" / "decedent_last_name" / "decedent_middle_name" — \
component parts. Suffix (Jr/Sr/II/III) goes in middle_name if separate field needed.
- "decedent_city": city where the decedent lived/died (empty string if not stated)
- "decedent_state": state where the decedent lived/died (e.g. "Alabama"). \
If multiple states are mentioned (e.g. born in TX, died in AL), use where they DIED.
- "decedent_age_at_death": integer age at death (0 if not stated)
- "date_of_death": YYYY-MM-DD if found, otherwise empty string
- "decedent_obit_address": street address (e.g. "123 Main St") if the \
obituary explicitly states where the decedent lived. Empty if not stated. \
Do NOT guess.

— Family graph —
- "all_survivors": array of {{name, relationship, city}} objects for ALL \
named survivors. Include even those without cities (set city to empty string). \
Use empty array if no survivors are listed. Common relationships: spouse, son, \
daughter, brother, sister, grandchild, niece, nephew, parent, in-laws.
- "spouse_name": name of the surviving spouse if any (empty if no spouse \
listed or spouse predeceased the decedent).
- "preceded_in_death": array of names of family members who predeceased the \
decedent. Common phrasing: "preceded in death by", "predeceased by". Use \
empty array if none stated.
- "executor_named": name of the executor / personal representative if the \
obituary explicitly names one (e.g. "John Smith, executor of the estate"). \
Empty string if not explicitly named.

Important: This is a pre-probate workflow — there is NO court case yet. \
The actionable contact will be derived from this family graph (executor → \
spouse → children → siblings) downstream. Be thorough on survivors.

Obituary text:
{obituary_text}"""


@dataclass
class DecedentExtraction:
    """Output of the per-obit LLM extraction."""

    is_obituary: bool = False
    decedent_full_name: str = ""
    decedent_first_name: str = ""
    decedent_last_name: str = ""
    decedent_middle_name: str = ""
    decedent_city: str = ""
    decedent_state: str = ""
    decedent_age_at_death: int = 0
    date_of_death: str = ""
    decedent_obit_address: str = ""
    all_survivors: list[dict] = field(default_factory=list)
    spouse_name: str = ""
    preceded_in_death: list[str] = field(default_factory=list)
    executor_named: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _extract_decedent_with_llm(
    obituary_text: str,
    api_key: str | None = None,
    *,
    rate_tracker: ServiceRateTracker | None = None,
) -> Optional[DecedentExtraction]:
    """Call Claude Haiku on a single obituary page and return parsed result.

    The ``rate_tracker`` kwarg threads through to ``llm_client.chat_json``
    (Wave 2 contract) so LLM extraction outcomes feed the per-run + 7-day
    rolling rates. Required keys for this prompt's success contract:
    ``is_obituary`` + ``decedent_full_name`` — chat_json records success
    only when BOTH are present in the parsed response.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or not obituary_text or not obituary_text.strip():
        return None
    prompt = DECEDENT_PROMPT.format(obituary_text=obituary_text[:MAX_OBITUARY_TEXT])
    try:
        parsed = llm_client.chat_json(
            prompt,
            system=SYSTEM_PROMPT,
            max_tokens=LLM_MAX_TOKENS,
            api_key=api_key,
            rate_tracker=rate_tracker,
            required_keys=("is_obituary", "decedent_full_name"),
        )
    except Exception as e:
        logger.debug("LLM call failed: %s", e)
        return None
    if not parsed or not parsed.get("is_obituary"):
        return None

    survivors = parsed.get("all_survivors") or []
    survivors = [s for s in survivors if isinstance(s, dict) and s.get("name")]
    pid = parsed.get("preceded_in_death") or []
    pid = [str(n).strip() for n in pid if str(n).strip()]
    try:
        age = int(parsed.get("decedent_age_at_death") or 0)
    except (TypeError, ValueError):
        age = 0

    return DecedentExtraction(
        is_obituary=True,
        decedent_full_name=(parsed.get("decedent_full_name") or "").strip(),
        decedent_first_name=(parsed.get("decedent_first_name") or "").strip(),
        decedent_last_name=(parsed.get("decedent_last_name") or "").strip(),
        decedent_middle_name=(parsed.get("decedent_middle_name") or "").strip(),
        decedent_city=(parsed.get("decedent_city") or "").strip(),
        decedent_state=(parsed.get("decedent_state") or "").strip(),
        decedent_age_at_death=age,
        date_of_death=(parsed.get("date_of_death") or "").strip(),
        decedent_obit_address=(parsed.get("decedent_obit_address") or "").strip(),
        all_survivors=survivors,
        spouse_name=(parsed.get("spouse_name") or "").strip(),
        preceded_in_death=pid,
        executor_named=(parsed.get("executor_named") or "").strip(),
    )


# ── Result schema ────────────────────────────────────────────────────


MAX_DOD_AGE_YEARS = 2.0  # Reject obits whose DOD is more than this many years
                         # before today. Pre-probate is FRESH-LEADS only —
                         # 6-year-old obits are stale (probate long since
                         # closed, family long since dispersed).


@dataclass
class PreProbateResult:
    """End-to-end outcome for one harvested obituary."""

    obituary_url: str
    obituary_source: str  # "legacy.com" or "obits.al.com"
    county_hint: str = ""  # "Jefferson" | "Madison" | "" — from harvester

    # Decedent extraction
    obit_fetched: bool = False
    extraction: Optional[DecedentExtraction] = None

    # Property lookup
    property_lookup_attempted: bool = False
    property_found: bool = False
    matched_county: str = ""  # actual county the parcel resolved to
    parcel_id: str = ""
    situs_address: str = ""
    situs_city: str = ""
    situs_zip: str = ""
    is_homestead: bool = False
    total_value: float = 0.0
    is_delinquent: bool = False
    municipality: str = ""

    # Tier
    tier: Optional[int] = None
    in_target_zip: bool = False

    # Disposition
    status: str = "unknown"  # enriched | dropped_off_target | dropped_no_property |
                             # dropped_not_obituary | dropped_fetch_failed | error
    notes: str = ""

    @property
    def decedent_name(self) -> str:
        return self.extraction.decedent_full_name if self.extraction else ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.extraction is not None:
            d["extraction"] = self.extraction.to_dict()
        return d


# ── Property attachment (mirrors benchmark_pipeline_al._attach_property) ─


# Suffixes / titles to strip before generating the decedent dedup key.
# Without this strip "John Smith Jr." + "John Smith" yield different keys.
_DEDUP_NAME_NOISE: frozenset[str] = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v",
    "mr", "mrs", "ms", "dr", "rev", "hon",
})


def _normalize_decedent_key(name: str, dod: str = "") -> str:
    """Generate a fuzzy-match dedup key for a decedent name + DoD.

    Catches the live-observed pattern where the same person appears under
    multiple legacy.com IDs with name spelling variants:
      Elisabeth Watts Whitten / Elizabeth Watts Whitten
      Cynthia Dawn Walker / Cynthia Lynn McKinney Walker

    Key = first-3-chars-of-first-name + last-name (last word) + dod.
    Lowercase, suffix-stripped, punctuation-stripped.

    Returns empty string when name is missing — caller treats as "always
    unique" (no dedup attempted).
    """
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z\s]", " ", name.lower())
    parts = [p for p in cleaned.split() if p and p not in _DEDUP_NAME_NOISE]
    if not parts:
        return ""
    first_prefix = parts[0][:3]
    last = parts[-1]
    return f"{first_prefix}|{last}|{dod or 'nodod'}"


# ── Backward-compat re-exports ────────────────────────────────────
# The AssuranceWeb (Madison + Marshall) Smarty ZIP-recovery helpers
# moved to address_standardizer.py on 2026-05-23 so the legacy
# main.py daily flow (via property_lookup.py) can share them too.
# Aliases kept so external callers and any module that still imports
# the underscore-prefixed names (distress_proxy_pipeline.py,
# apn_probate_pipeline_al.py prior to its own update) keep working.
# New code should import from address_standardizer directly.
from address_standardizer import (
    smarty_zip_for_assuranceweb_address,
    smarty_zip_for_madison_address,
    smarty_zip_for_marshall_address,
)
_smarty_zip_for_assuranceweb_address = smarty_zip_for_assuranceweb_address
_smarty_zip_for_madison_address = smarty_zip_for_madison_address
_smarty_zip_for_marshall_address = smarty_zip_for_marshall_address


def _attach_property_for_decedent(
    extraction: DecedentExtraction,
    county_hint: str = "",
    min_score: float = 0.5,
) -> tuple[object | None, list, str]:
    """Search county property APIs by extracted decedent name.

    Tries the hinted county first (from the obit's source listing — Birmingham
    obit → Jefferson, Huntsville obit → Madison), then falls back to the
    other county to catch cross-county property ownership (decedent died in
    Birmingham hospital but owned property in Huntsville, etc.).

    Returns (primary, scored_records, matched_county). matched_county is
    "Jefferson" | "Madison" | "" so the caller can populate notice.county.
    """
    name = extraction.decedent_full_name
    if not name:
        return (None, [], "")

    # Convert "First Middle Last" → "Last First Middle" for tax-roll lookup.
    # Both _search_jefferson and _search_madison handle the reorder + middle
    # truncation internally, so passing either format works.
    tokens = name.strip().split()
    query = " ".join([tokens[-1]] + tokens[:-1]) if len(tokens) >= 2 else name

    # Build the search order: hinted county first, then the others.
    # All three AL counties on the same enrichment path — county_hint is the
    # most likely match based on the obit's source listing, but cross-county
    # property ownership is real (decedent died in Huntsville hospital but
    # owned property in Boaz, etc.) so we always try all three with the hint
    # promoted to first position.
    all_routes = [
        ("Jefferson", _search_jefferson),
        ("Madison", _search_madison),
        ("Marshall", _search_marshall),
    ]
    if county_hint in {"Marshall", "Madison", "Jefferson"}:
        search_order = (
            [(c, fn) for c, fn in all_routes if c == county_hint]
            + [(c, fn) for c, fn in all_routes if c != county_hint]
        )
    else:
        search_order = all_routes

    # Sort key needs to work for both Jefferson and Madison records.
    # Jefferson: is_homestead (bool), total_value (float)
    # Madison:   is_buildable (bool), no total_value (use 0)
    # getattr fallback chain handles both schemas without an isinstance check.
    def _primary_signal(rec) -> bool:
        return bool(getattr(rec, "is_homestead", None)
                    or getattr(rec, "is_buildable", False))

    def _value_signal(rec) -> float:
        return float(getattr(rec, "total_value", 0) or 0)

    # ── Path 1: search by decedent name (primary) ──
    for county, search_fn in search_order:
        try:
            records = search_fn(query)
        except Exception as e:
            logger.warning("%s property API failed for %s: %s", county, name, e)
            continue
        if not records:
            continue

        scored = [(r, _score(query, r.owner_name)) for r in records]
        scored = [(r, s) for r, s in scored if s >= min_score]
        if not scored:
            continue

        scored.sort(
            key=lambda rs: (_primary_signal(rs[0]), rs[1], _value_signal(rs[0])),
            reverse=True,
        )
        return (scored[0][0], [r for r, _ in scored], county)

    # ── Path 2: fallback by obit-stated address ──
    # If the obituary explicitly gave us the decedent's residence (e.g.
    # "lived at 123 Main St"), search by situs address. Catches cases where
    # the property is held in a trust, LLC, or spouse's name (so it's NOT
    # findable by the decedent's name) but the address itself is on the
    # current tax roll.
    obit_addr = (extraction.decedent_obit_address or "").strip()
    if obit_addr:
        from jefferson_property_api import search_by_situs_address as _search_jeff_addr
        try:
            addr_recs = _search_jeff_addr(obit_addr)
        except Exception as e:
            logger.debug("Jefferson address-fallback failed for %r: %s", obit_addr, e)
            addr_recs = []
        if addr_recs:
            # Pick the homestead/highest-value match
            addr_recs.sort(
                key=lambda r: (_primary_signal(r), _value_signal(r)),
                reverse=True,
            )
            logger.info("  [addr-fallback] %s → %s @ %s",
                        name, addr_recs[0].owner_name, obit_addr)
            return (addr_recs[0], addr_recs, "Jefferson")

    # ── Path 3: fallback by spouse name ──
    # When the decedent's property is on title under the SPOUSE'S name (very
    # common — surviving spouse stays in the home, decedent never went on
    # title or was removed at some point), searching by spouse's name finds
    # the property where searching by decedent's name returned nothing.
    spouse = (extraction.spouse_name or "").strip()
    if spouse:
        spouse_tokens = spouse.split()
        if len(spouse_tokens) >= 2:
            spouse_query = " ".join([spouse_tokens[-1]] + spouse_tokens[:-1])
        else:
            spouse_query = spouse
        # Validate spouse-found records by checking the decedent's last name
        # appears in the property's owner_name (joint owner) — otherwise
        # we'd accept random spouse-name collisions as matches.
        decedent_last = tokens[-1].upper() if tokens else ""

        for county, search_fn in search_order:
            try:
                records = search_fn(spouse_query)
            except Exception as e:
                logger.debug("%s spouse-fallback failed for %r: %s", county, spouse, e)
                continue
            if not records:
                continue
            # Filter to records whose owner_name also mentions the decedent's
            # last name (proves it's the family property, not a same-named
            # stranger's house).
            kept = [
                r for r in records
                if decedent_last and decedent_last in r.owner_name.upper()
            ]
            if not kept:
                continue
            scored = [(r, _score(spouse_query, r.owner_name)) for r in kept]
            scored = [(r, s) for r, s in scored if s >= min_score]
            if not scored:
                continue
            scored.sort(
                key=lambda rs: (_primary_signal(rs[0]), rs[1], _value_signal(rs[0])),
                reverse=True,
            )
            logger.info("  [spouse-fallback] %s (spouse %s) → %s",
                        name, spouse, scored[0][0].owner_name)
            return (scored[0][0], [r for r, _ in scored], county)

    return (None, [], "")


# ── Pipeline ─────────────────────────────────────────────────────────


def run_pipeline(
    limit: int = 50,
    pages: int = 1,
    tier_filter: tuple[int, ...] = (1, 2),
    markets: tuple[str, ...] = ("Birmingham", "Huntsville"),
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[PreProbateResult], FunnelCounter, ServiceRateTracker]:
    """End-to-end pre-probate pipeline.

    Args:
        limit: Max obituaries to harvest PER MARKET.
        pages: Listing pages to walk per market.
        tier_filter: ZIP tiers to keep. (1, 2) = Tier 1 ∪ Tier 2. () = all.
        markets: Which Alabama markets to harvest. Default: both Birmingham
            (→ Jefferson property API) and Huntsville (→ Madison property API).
        funnel: Optional caller-supplied counter. When omitted, a fresh
            FunnelCounter("pre_probate", gates=...) is constructed per
            CONTEXT.md D-01 (9-gate sequence).
        rate_tracker: Optional caller-supplied tracker. Threaded through
            ``_extract_decedent_with_llm`` (LLM), ``_smarty_zip_for_*``
            (Smarty), and ``batch_skip_trace`` (Tracerfy) — all Wave 2
            entry points that accept the kwarg.

    Returns:
        ``(results, funnel, rate_tracker)`` — results + populated funnel
        + tracker so notify_slack can append funnel + rates blocks.
    """
    # CONTEXT.md D-01: pre_probate gate sequence (9 gates), pre-seeded so
    # the Slack block ALWAYS renders all 9 even when a stage emitted zero
    # records (zero-count gates are signal — "all dropped at llm_extracted"
    # must be visible).
    if funnel is None:
        funnel = FunnelCounter("pre_probate", gates=[
            "obits_harvested", "cross_source_deduped", "fetched",
            "llm_extracted", "dod_gated", "property_matched",
            "tier_gated", "tracerfy_matched", "datasift_uploaded",
        ])
    if rate_tracker is None:
        rate_tracker = ServiceRateTracker()

    obits = harvest_alabama(markets=markets, limit_per_market=limit, pages=pages)
    logger.info("Harvested %d obituary URL(s) across markets %s", len(obits), markets)
    funnel.set("obits_harvested", len(obits))

    results: list[PreProbateResult] = []
    # Cross-source dedupe: legacy.com person URLs upgrade to obits.al.com
    # URLs, and the harvester also pulls obits.al.com URLs directly from the
    # listing — so the same decedent can show up under two different harvest
    # entries that resolve to the SAME effective obituary. Track effective
    # URLs and skip duplicates.
    seen_effective: set[str] = set()
    # Same-person dedupe (P0 #2): the same decedent often appears under
    # MULTIPLE legacy.com person IDs (cross-syndication artifacts —
    # Whitten ×2, Gordon ×2, Gerndt ×2 observed in live testing). The
    # effective-URL dedup above catches same-URL-after-upgrade dups; this
    # second pass catches different-URL-same-person dups using a normalized
    # (first-3-chars-of-first-name, last-name, DoD) key. Applied AFTER LLM
    # extraction (need a parsed name) but BEFORE the expensive property
    # lookup — saves API calls + prevents duplicate KEEPs in the CSV.
    seen_decedents: set[str] = set()
    # Same-property dedupe (P0 #3): occasionally two distinct obituary
    # writeups for the same person (or genuinely-different decedents sharing
    # an estate — co-deceased spouses, etc.) land on the same situs address.
    # E.g. "Cynthia Dawn Walker" + "Cynthia Lynn McKinney Walker" both at
    # 5184 CROWLEY DR. For marketing purposes the address is the lead, so
    # dedupe at the address level too. Applied AFTER property match.
    seen_addresses: set[tuple[str, str]] = set()
    for obit in obits:
        result = PreProbateResult(
            obituary_url=obit.url,
            obituary_source=obit.source,
            county_hint=obit.county_hint,
        )

        # Stage 1: fetch obit page text (auto-upgrade legacy.com previews
        # to the obits.al.com full text if available).
        text, effective_url = _fetch_full_obit_text(obit.url)
        if not text or len(text) < 200:
            result.status = "dropped_fetch_failed"
            result.notes = f"obit fetch failed (len={len(text) if text else 0})"
            results.append(result)
            continue

        # Skip if we've already processed this effective URL via another path.
        # P1 #6: Also canonicalize by obit ID — two forms of the same obit
        # (legacy.com /us/obituaries/.../?id=N vs obits.al.com /us/obituaries/
        # .../?id=N) share the same numeric ID but produce different
        # effective URLs. Match on the ID to dedupe before paying the LLM
        # cost. The post-LLM same-person dedup (P0 #2) catches the
        # /person/ID variants where the ID space differs across hosts.
        url_id_match = re.search(r"[?&]id=(\d+)", effective_url)
        id_canonical = f"obit_id:{url_id_match.group(1)}" if url_id_match else ""
        if effective_url in seen_effective or (
            id_canonical and id_canonical in seen_effective
        ):
            result.status = "dropped_duplicate"
            result.notes = f"duplicate of effective URL {effective_url}"
            logger.debug("  SKIP %s: duplicate of %s", obit.url[-40:], effective_url)
            results.append(result)
            continue
        seen_effective.add(effective_url)
        if id_canonical:
            seen_effective.add(id_canonical)

        result.obit_fetched = True
        # Track the effective URL we actually extracted from
        if effective_url != obit.url:
            result.obituary_url = effective_url
            if "obits.al.com" in effective_url:
                result.obituary_source = "obits.al.com"

        # Stage 2: LLM extract decedent + family graph (rate_tracker
        # threads through to llm_client.chat_json — Wave 2 contract).
        ext = _extract_decedent_with_llm(text, rate_tracker=rate_tracker)
        if not ext or not ext.decedent_full_name:
            result.status = "dropped_not_obituary"
            result.notes = "LLM rejected (not a single-decedent obituary)"
            results.append(result)
            continue
        result.extraction = ext

        # Stage 2.4: Same-person dedupe (P0 #2).
        # Generates a fuzzy key on (first-3-chars + last-name + DoD) so
        # name-variant duplicates merge (Elisabeth vs Elizabeth, Dawn vs Lynn
        # McKinney as middle, etc.). Empty name → no dedup attempted.
        decedent_key = _normalize_decedent_key(
            ext.decedent_full_name, ext.date_of_death or "",
        )
        if decedent_key and decedent_key in seen_decedents:
            result.status = "dropped_duplicate_decedent"
            result.notes = (
                f"duplicate decedent (key={decedent_key}) — already processed"
            )
            logger.info("  SKIP %s (dup decedent): %s",
                        obit.url[-30:], ext.decedent_full_name)
            results.append(result)
            continue
        if decedent_key:
            seen_decedents.add(decedent_key)

        # Stage 2.5: DoD freshness gate. Pre-probate is fresh-leads only —
        # the legacy.com listing occasionally surfaces multi-year-old obits
        # (re-published, syndicated, or cached). A 6-year-old DoD means
        # probate is long since closed and the family long since dispersed,
        # so the lead isn't actionable. Drop it before paying for property
        # lookups + Tracerfy.
        if ext.date_of_death:
            dod_dt = _parse_flexible_date(ext.date_of_death)
            if dod_dt:
                from datetime import datetime
                age_years = (datetime.now() - dod_dt).days / 365.25
                if age_years > MAX_DOD_AGE_YEARS:
                    result.status = "dropped_stale_dod"
                    result.notes = (
                        f"DoD {ext.date_of_death} is {age_years:.1f}y ago "
                        f"(max {MAX_DOD_AGE_YEARS}y) — stale lead"
                    )
                    logger.info("  DROP %s (stale dod %.1fy): %s",
                                obit.url[-30:], age_years, ext.decedent_full_name)
                    results.append(result)
                    continue

        # Stage 3: property API search by decedent (county-routed)
        result.property_lookup_attempted = True
        primary, _all, matched_county = _attach_property_for_decedent(
            ext, county_hint=obit.county_hint,
        )
        if primary is None:
            result.status = "dropped_no_property"
            result.notes = f"No AL parcel for '{ext.decedent_full_name}'"
            logger.info("  DROP %s (no property): %s", obit.url[-30:], ext.decedent_full_name)
            results.append(result)
            continue

        result.property_found = True
        result.matched_county = matched_county
        result.parcel_id = primary.parcel_number
        result.situs_address = primary.situs_address

        # Field shape differs between counties:
        # - Jefferson (E-Ring): full schema — city/zip/homestead/total_value
        # - Madison + Marshall (AssuranceWeb): street only, is_buildable
        #   instead of is_homestead, no city/zip/valuation in bulk response
        if matched_county in ("Madison", "Marshall"):
            result.situs_city = ""  # AssuranceWeb response lacks city
            result.situs_zip = ""   # ...and ZIP — recovered via Smarty below
            result.is_homestead = bool(getattr(primary, "is_buildable", False))
            result.total_value = 0.0  # No valuation in bulk response
            result.is_delinquent = bool(getattr(primary, "is_delinquent", False))
            result.municipality = ""
        else:
            result.situs_city = primary.situs_city
            result.situs_zip = primary.situs_zip
            result.is_homestead = primary.is_homestead
            result.total_value = primary.total_value
            result.is_delinquent = primary.is_delinquent
            result.municipality = primary.municipality

        # Stage 3.5: Madison + Marshall records have no ZIP from the property
        # API. One-shot Smarty geocode to recover (city, zip) before the tier
        # gate. Anchor city differs by county (Huntsville for Madison,
        # Albertville for Marshall) so Smarty's USPS-CASS lookup resolves
        # rural addresses to the correct delivery city.
        if not result.situs_zip and result.situs_address:
            if matched_county == "Marshall":
                city, zip_code = _smarty_zip_for_marshall_address(
                    result.situs_address, rate_tracker=rate_tracker,
                )
                anchor = "Marshall"
            elif matched_county == "Madison":
                city, zip_code = _smarty_zip_for_madison_address(
                    result.situs_address, rate_tracker=rate_tracker,
                )
                anchor = "Madison"
            else:
                city, zip_code = ("", "")
                anchor = ""
            if zip_code:
                result.situs_zip = zip_code
                if not result.situs_city and city:
                    result.situs_city = city
                logger.debug("  Smarty geocode filled %s ZIP: %s → %s, %s",
                             anchor, result.situs_address, city, zip_code)

        # Stage 3.7: Same-property dedupe (P0 #3).
        # When two decedent obits resolve to the same situs address (e.g.
        # Cynthia Dawn Walker + Cynthia Lynn McKinney Walker both at 5184
        # CROWLEY DR), keep the first and skip the rest. The address is
        # the lead for marketing purposes; duplicate decedents at the same
        # property would inflate the DataSift list and waste Tracerfy spend.
        # Key = (uppercased situs_address, zip5) — both required so units
        # with same street but different ZIP don't collide.
        addr_key_parts = (
            (result.situs_address or "").strip().upper(),
            (result.situs_zip or "").strip()[:5],
        )
        if addr_key_parts[0] and addr_key_parts in seen_addresses:
            result.status = "dropped_duplicate_property"
            result.notes = (
                f"duplicate property — already kept {addr_key_parts[0]} "
                f"@ {addr_key_parts[1] or 'no-zip'}"
            )
            logger.info("  SKIP %s (dup property %s): %s",
                        obit.url[-30:], addr_key_parts[0],
                        ext.decedent_full_name)
            results.append(result)
            continue
        if addr_key_parts[0]:
            seen_addresses.add(addr_key_parts)

        # Stage 4: ZIP tier gate
        tier, _zone_county = zip_tier_county(result.situs_zip)
        result.tier = tier

        if tier_filter and (tier is None or tier not in tier_filter):
            result.status = "dropped_off_target"
            result.notes = f"ZIP {result.situs_zip or '(empty)'} not in tier filter {tier_filter}"
            logger.info("  DROP %s (tier=%s, zip=%s, county=%s): %s",
                        obit.url[-30:], tier, result.situs_zip or "?", matched_county,
                        ext.decedent_full_name)
            results.append(result)
            continue

        result.in_target_zip = True
        result.status = "enriched"
        logger.info("  KEEP T%s zip=%s county=%s: %s @ %s",
                    tier, result.situs_zip, matched_county,
                    ext.decedent_full_name, result.situs_address)
        results.append(result)

    # Funnel: end-of-loop gate counts (D-01 — 9 gates total).
    # obits_harvested already set above (line ~668). The remaining 7
    # gates are derived from the result list, walking each stage's
    # disposition:
    #
    #   cross_source_deduped  = obits_harvested - dropped_duplicate
    #   fetched               = obit_fetched True
    #   llm_extracted         = extraction is not None
    #   dod_gated             = status != dropped_stale_dod (within fetched+
    #                           llm-extracted survivors)
    #   property_matched      = property_found True
    #   tier_gated            = status == enriched
    #
    # tracerfy_matched + datasift_uploaded are populated downstream by
    # prepare_notices() and the CSV writer (pre-seeded to 0).
    dup_count = sum(1 for r in results if r.status == "dropped_duplicate")
    fetched_count = sum(1 for r in results if r.obit_fetched)
    llm_count = sum(1 for r in results if r.extraction is not None)
    # dod_gated is the count of LLM-extracted survivors that passed the
    # 2-year DoD freshness check. Anything that hit the dod_gated drop
    # subtracts; everything else with extraction survives the gate.
    stale_dod = sum(1 for r in results if r.status == "dropped_stale_dod")
    dod_gated_count = llm_count - stale_dod
    property_count = sum(1 for r in results if r.property_found)
    in_tier_count = sum(1 for r in results if r.status == "enriched")

    funnel.set("cross_source_deduped", len(results) - dup_count)
    funnel.set("fetched", fetched_count)
    funnel.set("llm_extracted", llm_count)
    funnel.set("dod_gated", dod_gated_count)
    funnel.set("property_matched", property_count)
    funnel.set("tier_gated", in_tier_count)

    return (results, funnel, rate_tracker)


# ── DataSift CSV conversion ──────────────────────────────────────────


def _to_notice_data(r: PreProbateResult) -> NoticeData:
    """Convert an enriched PreProbateResult into a NoticeData ready for DataSift."""
    notice = NoticeData()
    ext = r.extraction
    if ext is None:
        return notice

    # ── Notice metadata ──
    notice.notice_type = "pre_probate"
    notice.notice_subtype = "obituary_driven"
    # Use the actual matched county (Jefferson or Madison) so DataSift's
    # county-based filter presets (e.g. "jefferson" / "madison" tags) fire
    # correctly. Falls back to the harvester's hint, then "Jefferson" as
    # the safest default when neither is set.
    notice.county = r.matched_county or r.county_hint or "Jefferson"
    notice.state = "AL"
    notice.received_date = date.today().strftime("%Y-%m-%d")
    notice.date_added = ext.date_of_death or notice.received_date
    notice.source_url = r.obituary_url

    # ── Property (situs) ──
    notice.address = r.situs_address
    notice.city = r.situs_city
    notice.zip = r.situs_zip
    notice.parcel_id = r.parcel_id
    notice.is_homestead = "Y" if r.is_homestead else ""
    notice.assessed_value = f"{r.total_value:.0f}" if r.total_value else ""
    notice.municipality = r.municipality

    # ── Decedent identity (also the owner — they owned the property) ──
    notice.decedent_name = ext.decedent_full_name
    _split_decedent_name(notice)
    notice.owner_name = ext.decedent_full_name
    _split_owner_name(notice)

    # ── Pre-probate-specific fields ──
    notice.owner_deceased = "yes"
    notice.date_of_death = ext.date_of_death or ""
    notice.obituary_url = r.obituary_url
    notice.obituary_source_type = "full_page"

    # ── Family graph → ranked decision-makers ──
    # decedent_name is passed through so the ranker can filter the self-DM
    # bug (extractor naming the decedent as their own "survivor") and role
    # words ("wife" leaked into the name field).
    survivors = ext.all_survivors or []
    try:
        dms = rank_decision_makers(
            survivors=survivors,
            executor_name=ext.executor_named,
            decedent_name=ext.decedent_full_name,
        )
    except Exception as e:
        logger.debug("rank_decision_makers failed: %s", e)
        dms = []

    if dms:
        primary = dms[0]
        notice.decision_maker_name = primary.get("name", "")
        notice.decision_maker_relationship = primary.get("relationship", "")
        notice.decision_maker_status = primary.get("status", "unverified")
        notice.decision_maker_source = primary.get("source", "obituary_survivors")

        living = sum(1 for d in dms if d.get("status") == "verified_living")
        unverified = sum(1 for d in dms if d.get("status") == "unverified")
        deceased = sum(1 for d in dms if d.get("status") == "verified_deceased")
        notice.heirs_verified_living = str(living) if living else ""
        notice.heirs_unverified = str(unverified) if unverified else ""
        notice.heirs_verified_deceased = str(deceased) if deceased else ""
        notice.heir_map_json = json.dumps(dms)

        signers = [d for d in dms if d.get("signing_authority")]
        notice.signing_chain_count = str(len(signers)) if signers else ""
        notice.signing_chain_names = ", ".join(
            d.get("name", "") for d in signers if d.get("name")
        )
        notice.dm_confidence = "medium" if living or unverified else "low"
    else:
        # No DMs extractable from obit — mark for manual research
        notice.dm_confidence = "low"
        notice.dm_confidence_reason = "no_survivors_named_in_obituary"
        notice.missing_data_flags = "no_decision_maker"

    return notice


def prepare_notices(
    results: list[PreProbateResult],
    enriched_only: bool = True,
    skip_trace: bool = False,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[NoticeData], dict | None]:
    """Convert pipeline results to NoticeData and optionally run skip-trace.

    When ``funnel`` is supplied, sets ``tracerfy_matched`` from skip-trace
    stats. When ``rate_tracker`` is supplied, it's threaded into
    ``batch_skip_trace`` (Wave 2 contract).
    """
    eligible = [r for r in results if not enriched_only or r.status == "enriched"]
    notices = [_to_notice_data(r) for r in eligible]
    stats: dict | None = None

    if skip_trace and notices:
        try:
            import tracerfy_skip_tracer
            stats = tracerfy_skip_tracer.batch_skip_trace(
                notices, rate_tracker=rate_tracker,
            )
            logger.info(
                "Skip-trace stats: submitted=%d matched=%d phones=%d emails=%d cost=$%.2f",
                stats.get("submitted", 0), stats.get("matched", 0),
                stats.get("phones_found", 0), stats.get("emails_found", 0),
                stats.get("cost", 0.0),
            )
            for n in notices:
                _promote_heir_contacts_to_csv_slots(n)
            if funnel is not None:
                funnel.set("tracerfy_matched", stats.get("matched", 0))
        except Exception as e:
            logger.warning("Skip-trace failed (continuing without phones): %s", e)
            stats = {"error": str(e)}

    return notices, stats


# ── Slack notification ───────────────────────────────────────────────


def build_slack_message(
    results: list[PreProbateResult],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
) -> str:
    """Build a concise pre-probate Slack notification.

    Same shape as post-probate's notification but with pre-probate semantics:
    decedent + obit + extracted DM (no court-PR fallback since no court
    case exists yet).
    """
    total = len(results)
    by_status = Counter(r.status for r in results)
    enriched = [r for r in results if r.status == "enriched"]
    by_tier = Counter(r.tier for r in enriched)

    lines: list[str] = []
    today = date.today().strftime("%Y-%m-%d")
    lines.append(f"*Alabama Pre-Probate — {today}*")
    lines.append(
        f"  harvested: {total}  ·  in-tier: {len(enriched)} "
        f"(T1: {by_tier.get(1, 0)}  T2: {by_tier.get(2, 0)})  ·  "
        f"off-tier: {by_status.get('dropped_off_target', 0)}  ·  "
        f"no-property: {by_status.get('dropped_no_property', 0)}  ·  "
        f"not-obit: {by_status.get('dropped_not_obituary', 0)}  ·  "
        f"stale: {by_status.get('dropped_stale_dod', 0)}  ·  "
        f"dupes: {by_status.get('dropped_duplicate', 0)}  ·  "
        f"fetch-fail: {by_status.get('dropped_fetch_failed', 0)}"
    )

    if not enriched:
        lines.append("")
        lines.append("_No new in-tier pre-probate leads this run._")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"*New leads — {len(enriched)}*")

    for r in enriched:
        ext = r.extraction
        if not ext:
            continue
        addr = r.situs_address or "(address unknown)"
        val = f"${r.total_value:,.0f}" if r.total_value else "$?"
        tier_label = f"T{r.tier}" if r.tier else "T?"
        county_label = r.matched_county or r.county_hint or "?"
        tier_label = f"{tier_label}·{county_label}"
        flags: list[str] = []
        if r.is_homestead:
            flags.append("homestead")
        if r.is_delinquent:
            flags.append("delinquent")
        flag_str = "  ·  " + " · ".join(flags) if flags else ""

        lines.append("")
        lines.append(
            f"• *{addr}*, {r.situs_city} {r.situs_zip}  ·  {tier_label}  ·  {val}{flag_str}"
        )
        age = f", age {ext.decedent_age_at_death}" if ext.decedent_age_at_death else ""
        dod = f", DoD {ext.date_of_death}" if ext.date_of_death else ""
        lines.append(f"    decedent: *{ext.decedent_full_name}*{age}{dod}")

        if ext.spouse_name:
            lines.append(f"    spouse: {ext.spouse_name}")
        if ext.executor_named:
            lines.append(f"    executor named: {ext.executor_named}")
        if ext.all_survivors:
            lines.append(f"    survivors: {len(ext.all_survivors)} listed")
        if ext.preceded_in_death:
            lines.append(f"    predeceased: {', '.join(ext.preceded_in_death[:3])}")
        lines.append(f"    obit: <{r.obituary_url}|{r.obituary_source}>")

    if skip_trace_stats:
        sub = skip_trace_stats.get("submitted", 0)
        if sub:
            matched = skip_trace_stats.get("matched", 0)
            ph = skip_trace_stats.get("phones_found", 0)
            em = skip_trace_stats.get("emails_found", 0)
            cost = skip_trace_stats.get("cost", 0.0)
            lines.append("")
            lines.append(
                f"*Skip-trace:* {matched}/{sub} contacts matched  ·  "
                f"{ph} phones, {em} emails  ·  ${cost:.2f}"
            )

    if csv_path:
        lines.append("")
        lines.append(f"*CSV:* `{csv_path.name}` — {csv_path.parent}")

    return "\n".join(lines)


def notify_slack(
    results: list[PreProbateResult],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
    webhook_url: str | None = None,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> bool:
    """Post the pre-probate run summary to Slack/Discord.

    When ``funnel`` and ``rate_tracker`` are BOTH provided (Phase 2
    block-aware path):

      1. load_rolling_rates() FIRST — today's post shows the baseline
         from yesterday-and-prior days.
      2. Build 3-block payload: existing summary text + funnel block +
         service-rates block.
      3. POST via _send_blocks_webhook (single HTTP call, D-02).
      4. save_rolling_rates AFTER successful send so today's totals
         advance the window for tomorrow's baseline. A failed send
         leaves the rolling baseline untouched.

    Legacy callers (no funnel + no tracker) get the byte-identical
    plain-text-only path through _send_webhook.
    """
    text = build_slack_message(
        results, csv_path=csv_path, skip_trace_stats=skip_trace_stats,
    )

    # Legacy text-only path — backwards compat.
    if funnel is None and rate_tracker is None:
        import slack_notifier
        sent = slack_notifier._send_webhook(text, webhook_url=webhook_url)
        if sent:
            logger.info("Slack notification sent (%d enriched, legacy text-only)",
                        sum(1 for r in results if r.status == "enriched"))
        else:
            logger.warning("Slack notification failed (no webhook or send error)")
        return sent

    # Phase 2 block-aware path. Rolling-rates ordering (D-03 / W6):
    # load FIRST, save AFTER successful send.
    rolling = rolling_rates_summary(load_rolling_rates())
    per_run = rate_tracker.per_run_rates() if rate_tracker else {}

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    if funnel is not None:
        blocks.append(
            build_funnel_block(funnel.pipeline_name, funnel.as_ordered_dict())
        )
    blocks.append(build_service_rates_block(per_run, rolling))

    sent = _send_blocks_webhook(text, blocks, webhook_url=webhook_url)
    if sent and rate_tracker is not None:
        save_rolling_rates(rate_tracker.totals())
        logger.info(
            "Slack notification sent (%d enriched, blocks payload + rolling saved)",
            sum(1 for r in results if r.status == "enriched"),
        )
    elif sent:
        logger.info(
            "Slack notification sent (%d enriched, blocks payload)",
            sum(1 for r in results if r.status == "enriched"),
        )
    else:
        logger.warning("Slack notification failed (no webhook or send error)")

    return sent


# ── Reporting + CLI ──────────────────────────────────────────────────


def _print_summary(results: list[PreProbateResult]) -> None:
    total = len(results)
    by_status = Counter(r.status for r in results)
    enriched = [r for r in results if r.status == "enriched"]
    by_tier = Counter(r.tier for r in enriched)

    print(f"\n{'═' * 64}")
    print(f"  Pre-probate pipeline — {total} obit(s) processed")
    print(f"{'═' * 64}")
    print(f"  enriched (in target ZIP):     {by_status['enriched']}")
    print(f"    Tier 1:                     {by_tier.get(1, 0)}")
    print(f"    Tier 2:                     {by_tier.get(2, 0)}")
    print(f"  dropped (off-target ZIP):     {by_status.get('dropped_off_target', 0)}")
    print(f"  dropped (no AL property):     {by_status.get('dropped_no_property', 0)}")
    print(f"  dropped (not obituary):       {by_status.get('dropped_not_obituary', 0)}")
    print(f"  dropped (stale DoD):          {by_status.get('dropped_stale_dod', 0)}")
    print(f"  dropped (duplicate):          {by_status.get('dropped_duplicate', 0)}")
    print(f"  dropped (fetch failed):       {by_status.get('dropped_fetch_failed', 0)}")
    print()

    if enriched:
        print(f"  ━━━ Enriched leads ━━━")
        for r in enriched:
            ext = r.extraction
            print(f"  • {r.situs_address[:40]}, {r.situs_city} {r.situs_zip}  T{r.tier}  ${r.total_value:,.0f}")
            if ext:
                age = f", age {ext.decedent_age_at_death}" if ext.decedent_age_at_death else ""
                dod = f", DoD {ext.date_of_death}" if ext.date_of_death else ""
                print(f"      decedent: {ext.decedent_full_name}{age}{dod}")
                if ext.spouse_name:
                    print(f"      spouse: {ext.spouse_name}")
                if ext.all_survivors:
                    print(f"      survivors listed: {len(ext.all_survivors)}")
            print(f"      obit: {r.obituary_url}")
    print()


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Run the Jefferson AL pre-probate pipeline (obit harvest → property → ZIP gate → enrich → CSV).",
    )
    p.add_argument("--markets", type=str, default="Birmingham,Huntsville",
                   help="Comma-separated markets to harvest "
                        "(default: Birmingham,Huntsville). Choices: Birmingham, Huntsville.")
    p.add_argument("--limit", type=int, default=50,
                   help="Max obituaries to harvest PER MARKET (default: 50)")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to walk per market (default: 1)")
    p.add_argument("--tiers", type=str, default="1,2",
                   help="Comma-separated tier filter: '1', '2', '1,2', or 'none' (default: 1,2)")
    p.add_argument("--datasift-csv", action="store_true",
                   help="Write enriched results to a DataSift-formatted upload CSV.")
    p.add_argument("--skip-trace", action="store_true",
                   help="With --datasift-csv: also run Tracerfy skip-trace.")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post a run summary to Slack/Discord.")
    p.add_argument("--json", action="store_true",
                   help="Output structured JSON instead of summary.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "h2", "hpack", "primp", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.tiers.lower() == "none":
        tier_filter: tuple[int, ...] = ()
    else:
        try:
            tier_filter = tuple(int(t) for t in args.tiers.split(",") if t.strip())
        except ValueError:
            print(f"Invalid --tiers value: {args.tiers!r}", file=sys.stderr)
            return 2

    markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())

    results, funnel, rate_tracker = run_pipeline(
        limit=args.limit, pages=args.pages, tier_filter=tier_filter,
        markets=markets,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        _print_summary(results)

    csv_path: Optional[Path] = None
    skip_trace_stats: dict | None = None

    if args.datasift_csv:
        notices, skip_trace_stats = prepare_notices(
            results,
            skip_trace=args.skip_trace,
            funnel=funnel,
            rate_tracker=rate_tracker,
        )
        if notices:
            # Trestle phone scoring before CSV write — operator review
            # 2026-06-13 found pre-probate records arriving in DataSift
            # with empty Phone Tags N columns because this pipeline
            # never ran Trestle (only main.py daily via full_pipeline.py
            # did). Shared helper returns {} if TRESTLE_API_KEY isn't
            # set OR no record has phones, so the call is safe either way.
            from phone_validator import score_phones_for_pipeline
            phone_tiers = score_phones_for_pipeline(notices)
            csv_path = datasift_formatter.write_datasift_csv(
                notices, phone_tiers=phone_tiers,
            )
            # Funnel: datasift_uploaded gate — D-01 final stage.
            funnel.set("datasift_uploaded", len(notices))
            print(f"\n✓ DataSift CSV written: {csv_path}")
        else:
            print("\n· DataSift CSV: 0 eligible records.")
    elif args.skip_trace:
        print("\n· --skip-trace ignored (requires --datasift-csv).")

    # D-04 — terminal mirrors Slack: always log the funnel at end-of-run.
    logger.info(
        "Funnel (%s): %s",
        funnel.pipeline_name,
        dict(funnel.as_ordered_dict()),
    )

    if args.notify_slack:
        sent = notify_slack(
            results,
            csv_path=csv_path,
            skip_trace_stats=skip_trace_stats,
            funnel=funnel,
            rate_tracker=rate_tracker,
        )
        if sent:
            print(f"✓ Slack notification posted")
        else:
            print(f"· Slack notification failed (check SLACK_WEBHOOK_URL)")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
