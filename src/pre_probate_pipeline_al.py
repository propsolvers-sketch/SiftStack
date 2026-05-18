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
# them linked from a legacy.com preview. obits.al.com is highest priority
# (richest data + we have working parser); others sorted alphabetically.
_PARTNER_DOMAIN_PRIORITY = [
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
]

# Patterns to SKIP (CDN, social, analytics, ads).
# memoriams.com is a "place an obituary anywhere" advertising service legacy
# links to as cross-promotion — its pages contain marketing copy that's
# 6000+ chars (full length) but ZERO obit content. Always skip.
_PARTNER_SKIP_KEYWORDS = (
    "legacy.net", "memoriams.com", "facebook.com", "twitter.com",
    "googleapis", "tracking", "cloudfront", "amazonaws", "doubleclick",
    "media.legacy", "cache.legacy",
)

# Phrases that indicate the fetched text is a REAL obituary, not navigation
# chrome or an ad page. We require at least one match before accepting a
# partner-site fetch.
_OBIT_MARKERS = (
    "survived by", "preceded in death", "predeceased",
    "passed away", "passed peacefully", "passed quietly",
    "born on", "born in", "leaves behind",
    "celebration of life", "funeral service", "memorial service",
    "in lieu of flowers",
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


def _extract_partner_urls(legacy_person_url: str) -> list[str]:
    """Pull the raw legacy.com HTML and extract candidate partner-site URLs.

    Returns a list of URLs sorted by priority (obits.al.com first, then
    known funeral-home domains, then anything else that looks obit-related).
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

    # External hrefs (excluding legacy.com itself)
    hrefs = _re.findall(
        r'href=["\'](https?://(?!www\.legacy\.com)(?![\w\-]+\.legacy\.com)'
        r'(?!sympathy\.legacy)[^"\']+)["\']',
        r.text,
    )
    candidates: list[str] = []
    seen: set[str] = set()
    for h in hrefs:
        if h in seen:
            continue
        seen.add(h)
        h_lower = h.lower()
        if any(skip in h_lower for skip in _PARTNER_SKIP_KEYWORDS):
            continue
        # Must look obit-related
        if not any(k in h_lower for k in (
            "obituar", "memorial", "funeral", "tribute", "death",
        )):
            continue
        candidates.append(h)

    # Sort by priority — obits.al.com first, then known funeral-home domains
    # in priority order, then everything else alphabetically.
    def _priority(u: str) -> tuple[int, str]:
        u_lower = u.lower()
        for i, dom in enumerate(_PARTNER_DOMAIN_PRIORITY):
            if dom in u_lower:
                return (i, u_lower)
        return (len(_PARTNER_DOMAIN_PRIORITY), u_lower)

    candidates.sort(key=_priority)
    return candidates


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
        for partner_url in partner_urls[:4]:
            partner_text = _fetch_page_text(partner_url)
            if _looks_like_obituary(partner_text, min_chars=500):
                logger.debug("Partner-page upgrade: %s → %s (%d chars, obit-marker matched)",
                             url[-50:], partner_url[-60:], len(partner_text))
                return (partner_text, partner_url)
            else:
                logger.debug(
                    "Partner-page rejected (len=%d, no obit markers): %s — trying next",
                    len(partner_text) if partner_text else 0, partner_url,
                )

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
) -> Optional[DecedentExtraction]:
    """Call Claude Haiku on a single obituary page and return parsed result."""
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or not obituary_text or not obituary_text.strip():
        return None
    prompt = DECEDENT_PROMPT.format(obituary_text=obituary_text[:MAX_OBITUARY_TEXT])
    try:
        parsed = llm_client.chat_json(
            prompt, system=SYSTEM_PROMPT, max_tokens=LLM_MAX_TOKENS, api_key=api_key,
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


# Fallback anchor cities for AssuranceWeb ZIP recovery. The first attempt
# uses the primary anchor (Huntsville for Madison, Albertville for Marshall);
# if Smarty can't resolve the street there, we cycle through additional
# anchor cities AND a city-less retry. Each ~$0.001 — three lookups worst
# case for an address we'd otherwise drop, easily worth it.
_MADISON_ANCHORS: tuple[str, ...] = (
    "Huntsville AL", "Madison AL", "Athens AL", "Hazel Green AL",
    "New Hope AL", "Gurley AL", "Owens Cross Roads AL", "New Market AL",
    # Smaller Madison-area communities — each is a separate USPS catchment
    # so Smarty can't infer them from larger anchors. Verified that real
    # addresses (e.g. Carters Gin Rd) resolve under "Toney AL" but fail
    # under "Huntsville AL".
    "Toney AL", "Meridianville AL", "Harvest AL", "Triana AL",
    "AL",  # Final fallback: let Smarty pick the city from the street match
)
_MARSHALL_ANCHORS: tuple[str, ...] = (
    "Albertville AL", "Boaz AL", "Guntersville AL", "Arab AL",
    "Grant AL", "Horton AL", "Crossville AL",
    "AL",
)


def _smarty_lookup_once(situs: str, lastline_hint: str) -> tuple[str, str]:
    """Single Smarty lookup attempt. Returns ('','') on any failure."""
    try:
        import config as cfg
        if not (cfg.SMARTY_AUTH_ID and cfg.SMARTY_AUTH_TOKEN):
            return ("", "")
        from smartystreets_python_sdk.us_street import Lookup as StreetLookup
        from smartystreets_python_sdk.us_street.match_type import MatchType
        from address_standardizer import _build_client
        client = _build_client(cfg.SMARTY_AUTH_ID, cfg.SMARTY_AUTH_TOKEN)
        lookup = StreetLookup()
        lookup.street = situs.strip()
        lookup.lastline = lastline_hint
        lookup.candidates = 1
        lookup.match = MatchType.INVALID
        client.send_lookup(lookup)
        if not lookup.result:
            return ("", "")
        comp = lookup.result[0].components
        return (comp.city_name or "", comp.zipcode or "")
    except Exception as e:
        logger.debug("Smarty geocode failed for %r (lastline=%r): %s",
                     situs, lastline_hint, e)
        return ("", "")


def _smarty_zip_for_assuranceweb_address(
    situs: str,
    lastline_hint: str = "Huntsville AL",
    anchor_fallbacks: tuple[str, ...] | None = None,
) -> tuple[str, str]:
    """Multi-anchor Smarty lookup to recover (city, zip) for an AssuranceWeb situs.

    Madison + Marshall both run on the AssuranceWeb platform, and both
    `search_by_owner_name` responses return only the street — the city/zip
    portion isn't in the bulk search payload. We need ZIP for the tier gate,
    so geocode via Smarty's US Street API.

    Strategy (added to fix P1 #4 — multiple rural addresses dropped with
    zip=? in live runs: Lizotte ×3, Bell, M.Smith, Hudson, Manley, K.Floyd,
    Dova Hay):

      1. Try ``lastline_hint`` (e.g. "Huntsville AL") — handles the
         common case where the street is in the anchor city's catchment.
      2. If no match, cycle through ``anchor_fallbacks`` until one hits.
         These cover rural / fringe addresses where the anchor isn't
         geographically near the actual delivery city.
      3. Final attempt: lastline = "AL" alone — lets Smarty pick the city
         from the street match without any city bias.

    Returns ('', '') only when all fallbacks are exhausted.
    """
    if not situs or not situs.strip():
        return ("", "")

    # Try the primary anchor first
    city, zip_ = _smarty_lookup_once(situs, lastline_hint)
    if zip_:
        return (city, zip_)

    # Cycle through fallback anchors
    for anchor in (anchor_fallbacks or ()):
        if anchor == lastline_hint:
            continue  # Already tried the primary
        city, zip_ = _smarty_lookup_once(situs, anchor)
        if zip_:
            logger.debug("Smarty fallback hit on %r (anchor=%r): %s, %s",
                         situs, anchor, city, zip_)
            return (city, zip_)

    return ("", "")


def _smarty_zip_for_madison_address(situs: str) -> tuple[str, str]:
    """Madison-anchored ZIP recovery with multi-city fallback."""
    return _smarty_zip_for_assuranceweb_address(
        situs,
        lastline_hint="Huntsville AL",
        anchor_fallbacks=_MADISON_ANCHORS,
    )


def _smarty_zip_for_marshall_address(situs: str) -> tuple[str, str]:
    """Marshall-anchored ZIP recovery with multi-city fallback.

    Primary anchor is Albertville (largest city); fallbacks cycle through
    Boaz, Guntersville, Arab, Grant, Horton, Crossville, then city-less
    "AL" as the final attempt. Each retry ~$0.001 — cheap insurance for
    addresses Smarty's primary-anchor lookup can't resolve.
    """
    return _smarty_zip_for_assuranceweb_address(
        situs,
        lastline_hint="Albertville AL",
        anchor_fallbacks=_MARSHALL_ANCHORS,
    )


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
) -> list[PreProbateResult]:
    """End-to-end pre-probate pipeline.

    Args:
        limit: Max obituaries to harvest PER MARKET.
        pages: Listing pages to walk per market.
        tier_filter: ZIP tiers to keep. (1, 2) = Tier 1 ∪ Tier 2. () = all.
        markets: Which Alabama markets to harvest. Default: both Birmingham
            (→ Jefferson property API) and Huntsville (→ Madison property API).

    Returns one PreProbateResult per harvested obituary, including dropped
    ones (each tagged with disposition).
    """
    obits = harvest_alabama(markets=markets, limit_per_market=limit, pages=pages)
    logger.info("Harvested %d obituary URL(s) across markets %s", len(obits), markets)

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

        # Stage 2: LLM extract decedent + family graph
        ext = _extract_decedent_with_llm(text)
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
                city, zip_code = _smarty_zip_for_marshall_address(result.situs_address)
                anchor = "Marshall"
            elif matched_county == "Madison":
                city, zip_code = _smarty_zip_for_madison_address(result.situs_address)
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

    return results


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
) -> tuple[list[NoticeData], dict | None]:
    """Convert pipeline results to NoticeData and optionally run skip-trace."""
    eligible = [r for r in results if not enriched_only or r.status == "enriched"]
    notices = [_to_notice_data(r) for r in eligible]
    stats: dict | None = None

    if skip_trace and notices:
        try:
            import tracerfy_skip_tracer
            stats = tracerfy_skip_tracer.batch_skip_trace(notices)
            logger.info(
                "Skip-trace stats: submitted=%d matched=%d phones=%d emails=%d cost=$%.2f",
                stats.get("submitted", 0), stats.get("matched", 0),
                stats.get("phones_found", 0), stats.get("emails_found", 0),
                stats.get("cost", 0.0),
            )
            for n in notices:
                _promote_heir_contacts_to_csv_slots(n)
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
) -> bool:
    """Post the pre-probate run summary to Slack/Discord."""
    import slack_notifier
    text = build_slack_message(
        results, csv_path=csv_path, skip_trace_stats=skip_trace_stats,
    )
    sent = slack_notifier._send_webhook(text, webhook_url=webhook_url)
    if sent:
        logger.info("Slack notification sent (%d enriched leads)",
                    sum(1 for r in results if r.status == "enriched"))
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

    results = run_pipeline(
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
            results, skip_trace=args.skip_trace,
        )
        if notices:
            csv_path = datasift_formatter.write_datasift_csv(notices)
            print(f"\n✓ DataSift CSV written: {csv_path}")
        else:
            print("\n· DataSift CSV: 0 eligible records.")
    elif args.skip_trace:
        print("\n· --skip-trace ignored (requires --datasift-csv).")

    if args.notify_slack:
        sent = notify_slack(
            results, csv_path=csv_path, skip_trace_stats=skip_trace_stats,
        )
        if sent:
            print(f"✓ Slack notification posted")
        else:
            print(f"· Slack notification failed (check SLACK_WEBHOOK_URL)")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
