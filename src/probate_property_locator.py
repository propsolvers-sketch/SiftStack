"""Probate → property address enrichment for Jefferson + Madison Counties, AL.

A probate Notice-to-Creditors publication tells us *who* died and *who* manages
the estate, but never *what real estate* the decedent owned. This module fills
that gap by querying each county's public tax-roll API for parcels matching the
decedent (or PR) name.

Lookup waterfall:
  Tier 1: Search county tax roll by decedent name. Best signal — assessors
          record deceased owners with markers like "(D)", "(HEIRS OF)",
          "(ESTATE OF)", or "LIFE ESTATE".
  Tier 2: Search county tax roll by PR/Executor name. Catches family property
          where the deceased is a joint owner with the PR (common: surviving
          spouse becomes PR).

Scoring picks the best candidate by token overlap + deceased-marker bonus.
Tiers 3 (people search) and 4 (Tracerfy skip trace) are intentionally NOT
implemented here — they're already separate modules in the pipeline and can
be chained downstream.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Tokens that signal the assessor has flagged the owner as deceased
_DECEASED_MARKERS = ("(D)", "HEIRS OF", "ESTATE OF", "LIFE ESTATE")
# Generic words to drop when comparing owner-name tokens
_NAME_STOPWORDS = {
    "and", "or", "the", "of", "for", "to", "as",
    "etal", "et", "al", "jr", "sr", "ii", "iii", "iv",
    "trust", "trustee", "estate", "heirs", "life", "remainder",
    "agt", "agent", "co", "trustees",
}


@dataclass
class PropertyMatch:
    """Normalized match result combining either Jefferson or Madison record shape."""
    parcel_id: str
    owner_name: str          # As recorded on the tax roll
    address: str             # Property/situs address
    city: str
    state: str
    zip: str
    is_delinquent: bool
    deceased_flagged: bool   # Tax roll explicitly marks owner as deceased
    is_homestead: bool       # Likely owner-occupied primary residence
    total_value: float       # Total appraised value (0 if not reported)
    property_use: str        # "Residential" / "Commercial" / "Real Property" / etc.
    municipality: str        # Assessor's municipality code (Jefferson DispCode); "" for Madison
    score: float             # 0.0-1.0 — confidence
    source: str              # "decedent_name" or "pr_name"


@dataclass
class ProbateMatchSet:
    """Multi-parcel match: one primary residence + 0+ additional parcels owned by the same decedent."""
    primary: PropertyMatch
    additional: list[PropertyMatch]
    total_parcels: int
    total_estate_value: float

    def all_matches(self) -> list[PropertyMatch]:
        return [self.primary, *self.additional]


# ── Adapter protocol ─────────────────────────────────────────────────


class _PropertyRecord(Protocol):
    parcel_number: str
    owner_name: str
    is_delinquent: bool


# ── Name normalization + scoring ─────────────────────────────────────


def _tokens(name: str) -> set[str]:
    """Return the set of meaningful tokens in a name (lowercase, stopwords removed)."""
    if not name:
        return set()
    raw = re.sub(r"[^A-Za-z\s]", " ", name).lower()
    return {t for t in raw.split() if t and t not in _NAME_STOPWORDS and len(t) > 1}


def _has_deceased_marker(owner_name: str) -> bool:
    upper = owner_name.upper()
    return any(m in upper for m in _DECEASED_MARKERS)


def _score(query_name: str, record_owner: str) -> float:
    """Token-overlap score in [0, 1] between the query and the recorded owner.

    Bonus 0.2 added if the recorded owner has a deceased marker — these are
    almost always the right hit for a probate decedent.
    """
    q = _tokens(query_name)
    r = _tokens(record_owner)
    if not q or not r:
        return 0.0
    overlap = len(q & r)
    base = overlap / len(q)  # of the query's tokens, what fraction match
    if _has_deceased_marker(record_owner):
        base += 0.2
    return min(base, 1.0)


# ── County dispatch ──────────────────────────────────────────────────


def _search_jefferson(name: str) -> list[_PropertyRecord]:
    """Jefferson tax roll stores names as 'LAST FIRST MIDDLE' but probate notices
    write 'FIRST MIDDLE LAST'. Three retry layers handle the format gaps:

      1. Original form (handles tax-roll-format input)
      2. Last-first reordering (handles probate-notice format → tax roll)
      3. Truncate-to-LAST+FIRST (handles tax-roll storing middle as initial:
         e.g. probate says "FEREBEE ROBERT ALLEN" but tax roll has
         "FEREBEE ROBERT A JR & DEDE S")

    Caller is expected to score the combined results and reject low-confidence
    hits via min_score.
    """
    from jefferson_property_api import search_by_owner_name

    def _safe(q: str) -> list[_PropertyRecord]:
        try:
            return search_by_owner_name(q)
        except Exception as e:
            logger.warning("Jefferson search failed for %r: %s", q, e)
            return []

    seen: set[str] = set()
    combined: list[_PropertyRecord] = []

    def _accumulate(records: list[_PropertyRecord]) -> None:
        for r in records:
            pid = getattr(r, "parcel_number", "") or getattr(r, "parcel_id", "")
            if pid and pid not in seen:
                seen.add(pid)
                combined.append(r)

    queries: list[str] = [name]
    tokens = name.strip().split()

    # Last-first retry (probate-notice → tax-roll order)
    if len(tokens) >= 2:
        reordered = " ".join([tokens[-1]] + tokens[:-1])
        if reordered.upper() != name.strip().upper():
            queries.append(reordered)

    # Truncate-to-LAST+FIRST — handles middle-name abbreviation in the tax roll.
    # Apply to both orderings so we cover probate "FIRST MIDDLE LAST" and
    # tax-roll "LAST FIRST MIDDLE" inputs.
    if len(tokens) >= 3:
        queries.append(f"{tokens[0]} {tokens[1]}")              # tax-roll: LAST FIRST
        queries.append(f"{tokens[-1]} {tokens[0]}")             # probate: LAST FIRST

    for q in queries:
        records = _safe(q)
        if records:
            _accumulate(records)

    return combined


def _search_madison(name: str) -> list[_PropertyRecord]:
    """Madison tax roll stores names as 'LAST, FIRST MIDDLE' but probate
    notices write 'FIRST MIDDLE LAST'. Try both interpretations and combine
    deduplicated results — mirrors the Jefferson last-first retry pattern.

    The previous implementation passed `parts[0]` as the last name, which
    treated 'MARY ANGELA SMITH' as last='MARY' first='ANGELA SMITH' and
    silently returned 0 hits in nearly all probate-by-decedent searches.
    Madison probate property-locator hit rate was degraded relative to
    Jefferson with no metric flagging the gap.
    """
    from madison_property_api import search_by_owner_name

    # Comma form is unambiguous: 'LAST, FIRST MIDDLE'
    if "," in name:
        last, first = [s.strip() for s in name.split(",", 1)]
        try:
            return search_by_owner_name(last, first or None)
        except Exception as e:
            logger.warning("Madison search failed for %r: %s", name, e)
            return []

    parts = name.strip().split()
    if not parts:
        return []
    if len(parts) == 1:
        try:
            return search_by_owner_name(parts[0], None)
        except Exception as e:
            logger.warning("Madison search failed for %r: %s", name, e)
            return []

    # Two interpretations of multi-token input:
    #   A) 'FIRST MIDDLE LAST' (probate notice format — notice.decedent_name)
    #   B) 'LAST FIRST MIDDLE' (assessor / tax-roll format — PR-name fallback)
    interpretations = [
        (parts[-1], " ".join(parts[:-1])),  # A
        (parts[0], " ".join(parts[1:])),    # B
    ]

    seen_parcels: set[str] = set()
    combined: list[_PropertyRecord] = []
    for last, first in interpretations:
        try:
            for rec in search_by_owner_name(last, first or None):
                pid = getattr(rec, "parcel_number", "") or getattr(rec, "parcel_id", "")
                if pid and pid in seen_parcels:
                    continue
                if pid:
                    seen_parcels.add(pid)
                combined.append(rec)
        except Exception as e:
            logger.warning(
                "Madison search failed for last=%r first=%r: %s", last, first, e,
            )
            continue
    return combined


def _search_marshall(name: str) -> list[_PropertyRecord]:
    """Marshall tax roll is the same AssuranceWeb platform as Madison's, with
    the same 'LAST, FIRST MIDDLE' storage convention and the same comma-vs-space
    interpretation problem. The dispatcher mirrors `_search_madison` exactly —
    `MarshallPropertyRecord` is structurally identical to `MadisonPropertyRecord`
    so the downstream `_record_to_match()` works unchanged via getattr.
    """
    from marshall_property_api import search_by_owner_name

    if "," in name:
        last, first = [s.strip() for s in name.split(",", 1)]
        try:
            return search_by_owner_name(last, first or None)
        except Exception as e:
            logger.warning("Marshall search failed for %r: %s", name, e)
            return []

    parts = name.strip().split()
    if not parts:
        return []
    if len(parts) == 1:
        try:
            return search_by_owner_name(parts[0], None)
        except Exception as e:
            logger.warning("Marshall search failed for %r: %s", name, e)
            return []

    interpretations = [
        (parts[-1], " ".join(parts[:-1])),  # FIRST MIDDLE LAST (probate)
        (parts[0], " ".join(parts[1:])),    # LAST FIRST MIDDLE (tax-roll / PR fallback)
    ]

    seen_parcels: set[str] = set()
    combined: list[_PropertyRecord] = []
    for last, first in interpretations:
        try:
            for rec in search_by_owner_name(last, first or None):
                pid = getattr(rec, "parcel_number", "") or getattr(rec, "parcel_id", "")
                if pid and pid in seen_parcels:
                    continue
                if pid:
                    seen_parcels.add(pid)
                combined.append(rec)
        except Exception as e:
            logger.warning(
                "Marshall search failed for last=%r first=%r: %s", last, first, e,
            )
            continue
    return combined


def _adapter_for(county: str):
    c = county.strip().lower()
    if c == "jefferson":
        return _search_jefferson
    if c == "madison":
        return _search_madison
    if c == "marshall":
        return _search_marshall
    return None


def _record_to_match(rec, query_name: str, source: str) -> PropertyMatch:
    """Adapt either JeffersonPropertyRecord or MadisonPropertyRecord → PropertyMatch."""
    score = _score(query_name, rec.owner_name)
    deceased = _has_deceased_marker(rec.owner_name)

    # Jefferson exposes situs_address/city/state/zip + total_value + is_homestead;
    # Madison only exposes situs_address + is_buildable. Use getattr for both.
    address = getattr(rec, "situs_address", "")
    city = getattr(rec, "situs_city", "")
    state = getattr(rec, "situs_state", "AL")
    zip_ = getattr(rec, "situs_zip", "")
    parcel = getattr(rec, "parcel_number", "")
    total_value = float(getattr(rec, "total_value", 0) or 0)

    # Homestead: Jefferson has explicit signal; Madison falls back to is_buildable.
    homestead = bool(
        getattr(rec, "is_homestead", None)
        if hasattr(rec, "is_homestead")
        else getattr(rec, "is_buildable", False)
    )

    return PropertyMatch(
        parcel_id=parcel,
        owner_name=rec.owner_name,
        address=address,
        city=city,
        state=state,
        zip=zip_,
        is_delinquent=getattr(rec, "is_delinquent", False),
        deceased_flagged=deceased,
        is_homestead=homestead,
        total_value=total_value,
        property_use=getattr(rec, "property_use", ""),
        municipality=getattr(rec, "municipality", ""),
        score=score,
        source=source,
    )


# ── Tier 3 fallback: obit-cross-reference for spouse + obit address ──
# When neither the decedent name nor the PR name surface a parcel, look up an
# obituary for the decedent. The obit usually names a surviving spouse (often
# the actual on-title owner) and sometimes states the decedent's residence
# address directly. Two extra searches:
#   (a) by situs address from the obit  → catches property held in trust, LLC,
#       or spouse-only name
#   (b) by spouse name with last-name validation → catches property where the
#       decedent never went on title (common when one spouse is the deedholder)
# Both paths are gated on a successful obit fetch + LLM extraction. If the obit
# search fails, we return cleanly with no match — the upstream caller logs it.


def _obit_lookup_for_notice(notice: NoticeData):
    """Search for the decedent's obituary and LLM-extract spouse + obit address.

    Reuses the pre-probate path's DDG search + LLM parser. Returns a
    DecedentExtraction with at least decedent_full_name populated, or None.
    """
    if not notice.decedent_name:
        return None
    try:
        # Lazy import to avoid pulling LLM + DDG modules into pipelines that
        # never use the fallback (e.g. main.py daily without an LLM key).
        from obituary_enricher import _search_obituary, _fetch_page_text
        from pre_probate_pipeline_al import _extract_decedent_with_llm
    except Exception as e:
        logger.debug("Tier 3 obit imports failed: %s", e)
        return None

    state_full = "Alabama"  # All current pipelines are AL — TN expansion can override
    results = _search_obituary(
        notice.decedent_name, notice.city or "", state_full=state_full,
    )
    if not results:
        logger.debug("Tier 3: no obit hits for %r", notice.decedent_name)
        return None

    # Fetch and LLM-extract the first obit-domain hit
    for hit in results[:3]:
        url = hit.get("url", "")
        if not url:
            continue
        try:
            text = _fetch_page_text(url)
        except Exception as e:
            logger.debug("Tier 3 obit fetch failed for %s: %s", url, e)
            continue
        if not text or len(text) < 200:
            continue
        try:
            extraction = _extract_decedent_with_llm(text)
        except Exception as e:
            logger.debug("Tier 3 LLM extract failed for %s: %s", url, e)
            continue
        if extraction and extraction.is_obituary:
            # Cross-check name: decedent_full_name must share the last token
            # with notice.decedent_name to avoid same-name strangers.
            notice_last = notice.decedent_name.strip().split()[-1].upper()
            obit_last = (extraction.decedent_last_name
                         or extraction.decedent_full_name.split()[-1] if extraction.decedent_full_name
                         else "").upper()
            if notice_last and obit_last and notice_last != obit_last:
                logger.debug(
                    "Tier 3 obit last-name mismatch (notice=%s, obit=%s) — skipping",
                    notice_last, obit_last,
                )
                continue
            return extraction
    return None


def _situs_search_for_county(county: str, address: str) -> list:
    """Per-county situs-address search wrapper.

    Returns a list of raw county records (Jefferson/Madison/Marshall shape).
    Splits "123 Main St" into number + street-root for AssuranceWeb counties.
    """
    c = county.strip().lower()
    if not address or not address.strip():
        return []
    if c == "jefferson":
        from jefferson_property_api import search_by_situs_address as _j
        try:
            return _j(address)
        except Exception as e:
            logger.debug("Jefferson situs search failed for %r: %s", address, e)
            return []
    if c in ("madison", "marshall"):
        # AssuranceWeb counties want (number, name-root) as two args
        parts = address.strip().split(None, 1)
        if len(parts) < 2 or not parts[0][0].isdigit():
            return []
        number, name = parts[0], parts[1]
        api_module = "madison_property_api" if c == "madison" else "marshall_property_api"
        try:
            mod = __import__(api_module)
            return mod.search_by_situs_address(number, name)
        except Exception as e:
            logger.debug("%s situs search failed for %r: %s", county, address, e)
            return []
    return []


def _spouse_search_for_county(county: str, spouse_name: str, decedent_last: str) -> list:
    """Per-county owner-name search using the spouse's name, validated by the
    decedent's last name appearing in the matched record's owner_name.

    Filters out same-name-stranger matches by requiring the decedent's last
    name to appear in the matched owner_name (proves family property).
    """
    if not spouse_name or not spouse_name.strip() or not decedent_last:
        return []
    adapter = _adapter_for(county)
    if adapter is None:
        return []
    try:
        records = adapter(spouse_name)
    except Exception as e:
        logger.debug("%s spouse search failed for %r: %s", county, spouse_name, e)
        return []
    decedent_last_upper = decedent_last.upper()
    return [
        r for r in records
        if decedent_last_upper in (r.owner_name or "").upper()
    ]


# ── Public API ───────────────────────────────────────────────────────


def find_probate_property(
    notice: NoticeData,
    *,
    min_score: float = 0.5,
) -> PropertyMatch | None:
    """Single-parcel convenience wrapper around find_probate_properties().

    Returns the primary match (highest-confidence + homestead-preferred), or None.
    For full multi-parcel results use find_probate_properties().
    """
    match_set = find_probate_properties(notice, min_score=min_score)
    return match_set.primary if match_set else None


def find_probate_properties(
    notice: NoticeData,
    *,
    min_score: float = 0.5,
) -> ProbateMatchSet | None:
    """Search the county tax roll and return ALL parcels owned by the decedent.

    Same waterfall as find_probate_property() but returns the full set:
        primary    = best-guess primary residence (homestead-preferred)
        additional = remaining parcels with the same owner-name match
    A decedent who owns a homestead + rentals + family land surfaces all of them.
    """
    adapter = _adapter_for(notice.county)
    if adapter is None:
        logger.debug("No property adapter for county=%r", notice.county)
        return None
    if notice.notice_type != "probate":
        logger.debug("find_probate_properties called on non-probate notice; skipping")
        return None

    candidates: list[PropertyMatch] = []

    # Tier 1: decedent name
    if notice.decedent_name:
        for rec in adapter(notice.decedent_name):
            m = _record_to_match(rec, notice.decedent_name, source="decedent_name")
            if m.score >= min_score:
                candidates.append(m)

    # Tier 2: PR name (only if Tier 1 found nothing)
    if not candidates and notice.owner_name:
        for rec in adapter(notice.owner_name):
            m = _record_to_match(rec, notice.owner_name, source="pr_name")
            if m.score >= min_score:
                candidates.append(m)

    # Tier 3: obit cross-reference — search by spouse name and obit-stated
    # address. Runs only when 1+2 returned nothing because each step costs
    # one DDG search + one Firecrawl fetch + one LLM call.
    if not candidates:
        extraction = _obit_lookup_for_notice(notice)
        if extraction is not None:
            # Path A: obit-stated address → situs search
            obit_addr = (extraction.decedent_obit_address or "").strip()
            if obit_addr:
                for rec in _situs_search_for_county(notice.county, obit_addr):
                    m = _record_to_match(rec, notice.decedent_name, source="obit_address")
                    # Bypass min_score for situs-address matches — the address
                    # match is the signal, owner-name overlap may legitimately
                    # be low (property in spouse's or trust's name).
                    candidates.append(m)
                if candidates:
                    logger.info(
                        "  [obit-addr-fallback] %r → %s @ %s",
                        notice.decedent_name, candidates[0].owner_name, obit_addr,
                    )

            # Path B: spouse-name search with decedent-last-name validation
            spouse = (extraction.spouse_name or "").strip()
            decedent_last = (
                extraction.decedent_last_name
                or (notice.decedent_name.split()[-1] if notice.decedent_name else "")
            )
            if not candidates and spouse and decedent_last:
                for rec in _spouse_search_for_county(notice.county, spouse, decedent_last):
                    m = _record_to_match(rec, spouse, source="spouse_name")
                    if m.score >= min_score:
                        candidates.append(m)
                if candidates:
                    logger.info(
                        "  [obit-spouse-fallback] %r (spouse=%s) → %s",
                        notice.decedent_name, spouse, candidates[0].owner_name,
                    )

    if not candidates:
        logger.info(
            "No property match for probate notice (county=%s, decedent=%r, pr=%r)",
            notice.county, notice.decedent_name, notice.owner_name,
        )
        return None

    # De-duplicate by parcel_id (same parcel can match multiple ways)
    seen = set()
    unique: list[PropertyMatch] = []
    for c in candidates:
        if c.parcel_id and c.parcel_id in seen:
            continue
        seen.add(c.parcel_id)
        unique.append(c)

    # Pick primary residence:
    #   1. is_homestead AND deceased_flagged (best signal)
    #   2. highest total_value among homestead-flagged
    #   3. highest total_value among any
    unique.sort(
        key=lambda m: (
            m.is_homestead and m.deceased_flagged,
            m.is_homestead,
            m.total_value,
            m.score,
        ),
        reverse=True,
    )
    primary = unique[0]
    additional = unique[1:]
    total_value = sum(m.total_value for m in unique)

    logger.info(
        "Property match: %d parcels for %r (primary=%s, total_value=$%.0f)",
        len(unique), notice.decedent_name or notice.owner_name,
        primary.address, total_value,
    )
    return ProbateMatchSet(
        primary=primary,
        additional=additional,
        total_parcels=len(unique),
        total_estate_value=total_value,
    )


def enrich_notice_with_property(
    notice: NoticeData,
    *,
    min_score: float = 0.5,
) -> bool:
    """Run the multi-parcel lookup and write all matches onto the notice in place.

    Populates the primary parcel's address into ``address`` / ``city`` / ``state`` /
    ``zip`` / ``parcel_id`` / ``tax_owner_name`` / ``is_homestead`` (existing slots),
    plus the multi-parcel rollup into ``secondary_addresses`` (pipe-delimited) and
    ``total_estate_value`` (sum of all parcels). Returns True if a match was applied.
    """
    match_set = find_probate_properties(notice, min_score=min_score)
    if match_set is None:
        return False

    primary = match_set.primary
    if not notice.address:
        notice.address = primary.address
        notice.city = primary.city or notice.city
        notice.state = primary.state or notice.state or "AL"
        notice.zip = primary.zip or notice.zip
    if not notice.parcel_id:
        notice.parcel_id = primary.parcel_id
    if not notice.tax_owner_name:
        notice.tax_owner_name = primary.owner_name
    if primary.is_homestead and not notice.is_homestead:
        notice.is_homestead = "Y"

    # Last-assessed value of the PRIMARY parcel only (not the multi-parcel sum)
    if primary.total_value > 0 and not notice.assessed_value:
        notice.assessed_value = f"{primary.total_value:.0f}"
    # Assessor classification (Residential/Commercial/Real Property/Other)
    if primary.property_use and not notice.property_use:
        notice.property_use = primary.property_use
    # Municipality (Jefferson DispCode: BHAM, TRUSSVILLE, COUNTY, etc.; "" for Madison)
    if primary.municipality and not notice.municipality:
        notice.municipality = primary.municipality

    if match_set.additional and not notice.secondary_addresses:
        notice.secondary_addresses = " | ".join(
            f"{m.address}{f' ({m.city})' if m.city else ''}"
            for m in match_set.additional
            if m.address
        )
    if match_set.total_estate_value > 0 and not notice.total_estate_value:
        notice.total_estate_value = f"{match_set.total_estate_value:.0f}"

    # Survivor zip — best-effort from the existing decision-maker pipeline.
    # Set only when the DM is a different person from the PR (so we're capturing
    # an heir, not the petitioner). External skip-trace can override later.
    _populate_survivor_zip(notice)
    return True


def _populate_survivor_zip(notice: NoticeData) -> None:
    """Fill notice.survivor_zip from existing decision_maker_* fields when available.

    Only sets survivor_zip if the decision_maker_name is set AND is distinct from
    the PR (owner_name) — i.e., we identified a different family member as the
    decision maker. Otherwise leaves it empty for external skip-trace fill.
    """
    if notice.survivor_zip:
        return
    dm_zip = (notice.decision_maker_zip or "").strip()
    dm_name = (notice.decision_maker_name or "").strip().lower()
    pr_name = (notice.owner_name or "").strip().lower()
    if dm_zip and dm_name and dm_name != pr_name:
        notice.survivor_zip = dm_zip


# ── CLI for ad-hoc testing ───────────────────────────────────────────


def _main(argv: list[str]) -> int:
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if len(argv) < 2:
        print("Usage: probate_property_locator.py COUNTY DECEDENT_NAME [PR_NAME]", file=sys.stderr)
        print("       e.g. probate_property_locator.py Jefferson 'SMITH OPAL W'", file=sys.stderr)
        return 2

    notice = NoticeData(
        county=argv[0],
        notice_type="probate",
        decedent_name=argv[1],
        owner_name=argv[2] if len(argv) > 2 else "",
        state="AL",
    )
    match_set = find_probate_properties(notice)
    if match_set is None:
        print("(no match)")
        return 1

    p = match_set.primary
    print(f"\nPrimary residence (score={p.score:.2f}, source={p.source}):")
    print(f"  Parcel:    {p.parcel_id}")
    print(f"  Owner:     {p.owner_name}")
    print(f"  Address:   {p.address}, {p.city}, {p.state} {p.zip}")
    print(f"  Value:     ${p.total_value:,.0f}")
    print(f"  Homestead: {p.is_homestead}")
    print(f"  Deceased flagged: {p.deceased_flagged}")
    print(f"  Delinquent: {p.is_delinquent}")

    if match_set.additional:
        print(f"\nAdditional parcels ({len(match_set.additional)}):")
        for m in match_set.additional:
            print(f"  {m.parcel_id}  {m.address:30s}  ${m.total_value:>10,.0f}")

    print(f"\nTotal parcels: {match_set.total_parcels}")
    print(f"Total estate value: ${match_set.total_estate_value:,.0f}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
