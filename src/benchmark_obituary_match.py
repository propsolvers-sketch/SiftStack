"""Cross-reference Benchmark Web petitioner against decedent's obituary survivors.

Benchmark Web gives us the decedent + petitioner names but no addresses.
The decedent's obituary lists survivors with cities. By fuzzy-matching the
Benchmark petitioner against the survivor list, we recover the petitioner's
city — which then narrows the Jefferson property API search from "every
parcel in the county owned by anyone with this name" down to "parcels in
this specific city".

Public API:

    match_petitioner_city(case) -> MatchResult

where ``case`` is a ``BenchmarkCase`` from ``benchmark_web``. Returns the
matched survivor's city plus a confidence grade and the obituary URL used.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

import llm_client
from benchmark_web import BenchmarkCase
from obituary_enricher import (
    SYSTEM_PROMPT,
    _fetch_page_text,
    _search_obituary,
    rank_decision_makers,
)

if TYPE_CHECKING:
    from observability import ServiceRateTracker

# Phase 2 (OBS-01 / WARNING 7): the two routing keys the caller uses to
# grade confidence. `petitioner_match` ∈ {"exact","fuzzy","not_found"}
# routes the petitioner-as-survivor branch; `is_decedent_match` ∈
# {true,false} routes the right-decedent gate. Both MUST be present on
# every successful LLM response — if either is missing, the response is
# malformed and should count as an LLM failure (chat_json drops the
# result + records a failure into the rate tracker).
_OBIT_MATCH_REQUIRED_KEYS: tuple[str, ...] = (
    "is_decedent_match",
    "petitioner_match",
)

load_dotenv()

logger = logging.getLogger(__name__)

MAX_OBITUARIES_TO_TRY = 5
MAX_OBITUARY_TEXT = 6000
LLM_MAX_TOKENS = 1500

# Reject obituary/SSDI matches whose DOD is more than this many years before
# the probate filing date. Alabama probate is normally filed within months
# of death; a 17-year-old DOD is almost certainly a name collision (e.g.
# SSDI matched a different William Belew with the same name).
MAX_DOD_GAP_YEARS_FOR_FALLBACK = 3


def _parse_flexible_date(s: str) -> Optional["datetime"]:
    """Parse 'YYYY-MM-DD', 'DD Mon YYYY', 'Mon DD, YYYY', 'MM/DD/YYYY'."""
    from datetime import datetime
    s = (s or "").strip()
    if not s:
        return None
    fmts = ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _dod_within_window(dod_str: str, file_date_str: str, max_years: float) -> bool:
    """Return True if DOD is within ``max_years`` of the probate filing date.
    Returns True (permissive) if either date is unparseable or missing."""
    dod = _parse_flexible_date(dod_str)
    fdate = _parse_flexible_date(file_date_str)
    if not dod or not fdate:
        return True
    gap_years = (fdate - dod).days / 365.25
    if gap_years > max_years:
        return False
    if gap_years < -0.5:  # DOD after filing — invalid
        return False
    return True


SURVIVOR_PROMPT = """\
I have a probate case from Jefferson County, Alabama:
- Decedent: {decedent_name}
- Petitioner (filed the case): {petitioner_name}

Below is text from a candidate obituary. Determine if this obituary is for \
the decedent, locate the petitioner among the listed survivors, and extract \
the full family/contact picture.

Return a JSON object with these exact keys:

— Decedent identification —
- "is_decedent_match": true if this obituary is for {decedent_name}, false otherwise. \
Match on first AND last name; middle name is bonus confirmation. \
Be conservative — false negatives are better than false positives.
- "decedent_full_name": full name as printed in the obituary (empty string if no match)
- "decedent_city": city where the decedent lived/died (empty string if not stated)
- "decedent_state": state where the decedent lived/died (empty string if not stated)
- "decedent_age_at_death": integer age at death (0 if not stated)
- "date_of_death": YYYY-MM-DD if found, otherwise empty string
- "decedent_obit_address": street address (e.g. "123 Main St") if the obituary \
explicitly states where the decedent lived. Empty if not stated. Do NOT guess.

— Petitioner match —
- "petitioner_match": one of "exact", "fuzzy", "not_found".
  * "exact" — petitioner name appears verbatim in survivors (allowing for nickname \
or middle-name variation, e.g. "Tera Brooke Streufert" matches "Tera Streufert")
  * "fuzzy" — a survivor's first AND last name match the petitioner but with \
some divergence (e.g. married vs maiden surname, initial vs full middle name)
  * "not_found" — no survivor matches the petitioner name
- "petitioner_survivor_name": the matched survivor's name as printed (empty if not_found)
- "petitioner_relationship": their relationship to the decedent (e.g. "daughter", \
"son", "spouse", "niece"). Empty string if not_found or relationship not stated.
- "petitioner_city": the matched survivor's city as printed in the obituary. \
Empty string if not_found OR if the obituary doesn't state the survivor's city. \
DO NOT guess — only fill if explicitly stated next to the survivor's name.

— Family graph —
- "all_survivors": array of {{name, relationship, city}} objects for ALL named survivors. \
Include even those without cities (set city to empty string). Use empty array if no \
survivors are listed.
- "spouse_name": name of the surviving spouse if any (empty if no spouse listed or \
spouse predeceased the decedent). One of the survivors should also include them.
- "preceded_in_death": array of names of family members who predeceased the decedent. \
Common phrasing: "preceded in death by", "predeceased by". Use empty array if none stated.
- "executor_named": name of the executor / personal representative if the obituary \
explicitly names one (e.g. "John Smith, executor of the estate"). Empty string if not \
explicitly named — do NOT infer from the petitioner.

Important: The petitioner is the person who filed the probate case in court — they \
are almost always a close family member (spouse, adult child, sibling, niece/nephew). \
If you find the petitioner's first AND last name in the survivor list, that's a match.

Obituary text:
{obituary_text}"""


@dataclass
class MatchResult:
    """Outcome of matching a Benchmark petitioner against an obituary."""

    case_number: str
    decedent_name: str
    petitioner_name: str

    # Match grading
    confidence: str = "none"  # "high" / "medium" / "low" / "none"
    petitioner_match: str = "not_found"  # "exact" / "fuzzy" / "not_found"

    # Decedent identification
    decedent_full_name: str = ""
    decedent_city: str = ""
    decedent_state: str = ""
    decedent_age_at_death: int = 0
    decedent_dod: str = ""
    decedent_obit_address: str = ""  # if obituary explicitly states decedent's address

    # Petitioner-as-survivor
    petitioner_city: str = ""
    petitioner_relationship: str = ""
    petitioner_survivor_name: str = ""

    # Family graph
    all_survivors: list[dict] = field(default_factory=list)  # [{name, relationship, city}]
    spouse_name: str = ""
    preceded_in_death: list[str] = field(default_factory=list)
    executor_named: str = ""           # only if obituary explicitly names one
    heir_count: int = 0                # count of named survivors

    # Ranked decision-makers (from obituary_enricher.rank_decision_makers).
    # Each item: {name, relationship, rank, status, source, signing_authority}
    decision_makers: list[dict] = field(default_factory=list)

    # Audit trail
    obituary_url: str = ""
    obituaries_tried: int = 0
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def primary_dm_name(self) -> str:
        return self.decision_makers[0]["name"] if self.decision_makers else ""

    @property
    def primary_dm_relationship(self) -> str:
        return self.decision_makers[0]["relationship"] if self.decision_makers else ""


# ── Name normalization ────────────────────────────────────────────────


_LEGAL_SUFFIX_RE = re.compile(
    r"\b(JR|SR|II|III|IV|V|ESQ|MD|PHD|DDS|RN)\b\.?", re.IGNORECASE,
)


def _benchmark_name_to_human(raw: str) -> str:
    """Convert Benchmark "LAST, FIRST MIDDLE" → "First Middle Last".

    Examples:
        "STREUFERT, TERA BROOKE" → "Tera Brooke Streufert"
        "LANEY, HARVEY JACK"     → "Harvey Jack Laney"
        "BROWN, DAVID J"         → "David J Brown"
    """
    if not raw:
        return ""
    raw = _LEGAL_SUFFIX_RE.sub("", raw).strip().rstrip(",")
    raw = re.sub(r",\s*,", ",", raw).strip(" ,")
    if "," in raw:
        last, _, first = raw.partition(",")
        last = last.strip(" ,")
        first = first.strip(" ,")
    else:
        # Fallback: assume already First-Last form
        parts = raw.split()
        if len(parts) < 2:
            return raw.title()
        first = " ".join(parts[:-1])
        last = parts[-1]
    return f"{first.title()} {last.title()}".strip()


def _build_search_queries(
    decedent_name: str, county_hint: str = "Jefferson County"
) -> list[tuple[str, str]]:
    """Return a list of (query_name, city_hint) pairs to feed _search_obituary.

    First pass: name + county. Second pass: name + Birmingham (the dominant
    city in Jefferson County, AL). Third pass: name only.
    """
    return [
        (decedent_name, "Birmingham"),
        (decedent_name, county_hint),
        (decedent_name, ""),
    ]


# ── LLM call ──────────────────────────────────────────────────────────


def _parse_obituary_for_petitioner(
    obituary_text: str,
    decedent_name: str,
    petitioner_name: str,
    api_key: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> Optional[dict]:
    """Send the obituary text to Claude Haiku and ask for a structured match.

    Phase 2 (OBS-01 / WARNING 7): records the LLM call into the supplied
    rate_tracker via llm_client.chat_json's instrumentation, with
    required_keys = ("is_decedent_match", "petitioner_match"). Both keys
    must be present on the parsed JSON for chat_json to record success;
    a malformed response (missing either key) counts as a failure.
    """
    if not obituary_text.strip():
        return None
    prompt = SURVIVOR_PROMPT.format(
        decedent_name=decedent_name,
        petitioner_name=petitioner_name,
        obituary_text=obituary_text[:MAX_OBITUARY_TEXT],
    )
    try:
        parsed = llm_client.chat_json(
            prompt, system=SYSTEM_PROMPT, max_tokens=LLM_MAX_TOKENS, api_key=api_key,
            rate_tracker=rate_tracker,
            required_keys=_OBIT_MATCH_REQUIRED_KEYS,
        )
    except Exception as e:
        logger.debug("LLM call failed: %s", e)
        return None
    if not parsed:
        return None
    if not parsed.get("is_decedent_match"):
        return None
    return parsed


def _grade_confidence(parsed: dict) -> str:
    """Map LLM output → high/medium/low/none confidence band."""
    match = (parsed.get("petitioner_match") or "").lower()
    city = (parsed.get("petitioner_city") or "").strip()
    if match == "exact" and city:
        return "high"
    if match == "exact":
        return "medium"  # name matched, no city
    if match == "fuzzy" and city:
        return "medium"
    if match == "fuzzy":
        return "low"
    return "none"


# ── Public API ────────────────────────────────────────────────────────


def match_petitioner_city(
    case: BenchmarkCase,
    api_key: str | None = None,
    max_obituaries: int = MAX_OBITUARIES_TO_TRY,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> MatchResult:
    """Find the petitioner's city by cross-referencing the decedent's obituary.

    Args:
        case: A BenchmarkCase with at least decedent_name and petitioner_name
              populated (use ``BenchmarkCase.decedent_name`` / ``.petitioner_name``
              convenience properties).
        api_key: Anthropic API key (falls back to ANTHROPIC_API_KEY env var).
        max_obituaries: Max obituary URLs to fetch+parse before giving up.

    Returns:
        MatchResult — always returned, even if no match found
        (confidence="none" in that case).
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    decedent_human = _benchmark_name_to_human(case.decedent_name)
    petitioner_human = _benchmark_name_to_human(case.petitioner_name)

    result = MatchResult(
        case_number=case.case_number,
        decedent_name=decedent_human,
        petitioner_name=petitioner_human,
    )

    if not decedent_human or not petitioner_human:
        result.notes = "Missing decedent or petitioner name"
        return result

    if not api_key:
        result.notes = "No ANTHROPIC_API_KEY — skipping LLM match"
        return result

    # Collect candidate obituary URLs
    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for query_name, city_hint in _build_search_queries(decedent_human):
        hits = _search_obituary(query_name, city_hint, state_full="Alabama")
        for h in hits:
            url = h.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                candidates.append(h)
        if len(candidates) >= max_obituaries:
            break

    candidates = candidates[:max_obituaries]
    logger.info(
        "Searching %d obituary candidate(s) for %s (petitioner: %s)",
        len(candidates), decedent_human, petitioner_human,
    )

    best: Optional[dict] = None
    best_url = ""
    for cand in candidates:
        url = cand.get("url", "")
        result.obituaries_tried += 1
        text = _fetch_page_text(url)
        if not text or len(text) < 200:
            logger.debug("  Skipped (too short / fetch failed): %s", url)
            continue
        parsed = _parse_obituary_for_petitioner(
            text, decedent_human, petitioner_human, api_key,
            rate_tracker=rate_tracker,
        )
        if not parsed:
            logger.debug("  Not the right decedent: %s", url)
            continue

        # First confirmed obituary wins; if it has the petitioner, take it
        # and stop. Otherwise keep checking other candidates in case a more
        # complete obituary exists.
        if best is None:
            best, best_url = parsed, url
        if (parsed.get("petitioner_match") or "").lower() in ("exact", "fuzzy"):
            best, best_url = parsed, url
            break

    if best is None:
        result.notes = "No matching obituary found"
        return result

    result.obituary_url = best_url

    # Decedent identification
    result.decedent_full_name = (best.get("decedent_full_name") or "").strip()
    result.decedent_city = (best.get("decedent_city") or "").strip()
    result.decedent_state = (best.get("decedent_state") or "").strip()
    try:
        result.decedent_age_at_death = int(best.get("decedent_age_at_death") or 0)
    except (TypeError, ValueError):
        result.decedent_age_at_death = 0
    result.decedent_dod = (best.get("date_of_death") or "").strip()
    result.decedent_obit_address = (best.get("decedent_obit_address") or "").strip()

    # Petitioner match
    result.petitioner_match = (best.get("petitioner_match") or "not_found").lower()
    result.petitioner_survivor_name = (
        best.get("petitioner_survivor_name") or ""
    ).strip()
    result.petitioner_relationship = (
        best.get("petitioner_relationship") or ""
    ).strip()
    result.petitioner_city = (best.get("petitioner_city") or "").strip()

    # Family graph
    survivors = best.get("all_survivors") or []
    survivors = [s for s in survivors if isinstance(s, dict) and s.get("name")]
    result.all_survivors = survivors
    result.spouse_name = (best.get("spouse_name") or "").strip()
    pid = best.get("preceded_in_death") or []
    result.preceded_in_death = [str(n).strip() for n in pid if str(n).strip()]
    result.executor_named = (best.get("executor_named") or "").strip()
    result.heir_count = len(survivors)

    # Derived: ranked decision-makers. Pass decedent_name so the ranker
    # filters role-word leaks ("wife" as a name) AND the self-DM bug
    # (extractor naming the decedent as their own survivor).
    try:
        result.decision_makers = rank_decision_makers(
            survivors=survivors,
            executor_name=result.executor_named,
            decedent_name=result.decedent_full_name or result.decedent_name,
        )
    except Exception as e:
        logger.debug("rank_decision_makers failed: %s", e)
        result.decision_makers = []

    result.confidence = _grade_confidence(best)
    if result.confidence == "none":
        result.notes = "Obituary matched decedent but petitioner not in survivors"
    return result


# ── Ancestry / Newspapers.com fallback ───────────────────────────────


def _name_parts_for_ancestry(decedent_human: str) -> tuple[str, str]:
    """Convert "Robert Allen Ferebee" → ("Robert", "Ferebee") for Ancestry's
    first/last search fields. Middle name is dropped (Ancestry treats it as
    a separate filter, often hurts recall).
    """
    parts = (decedent_human or "").strip().split()
    if len(parts) < 2:
        return ("", "")
    return (parts[0], parts[-1])


async def enrich_via_ancestry(
    case: BenchmarkCase,
    existing: MatchResult,
    page,
    api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> MatchResult:
    """Fallback path: when DDG returned confidence=none, search Ancestry +
    Newspapers.com for the decedent's obituary, then run the SAME expanded
    LLM extraction on the recovered text.

    Args:
        case: The original BenchmarkCase.
        existing: The DDG-path MatchResult (mutated in place and returned).
        page: An already-open Ancestry-authenticated Playwright page.
        api_key: Anthropic key (falls back to env).

    Returns the (possibly upgraded) MatchResult.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        existing.notes = (existing.notes or "") + " [ancestry: no API key]"
        return existing

    import ancestry_enricher  # local import — heavy module

    decedent_human = _benchmark_name_to_human(case.decedent_name)
    petitioner_human = _benchmark_name_to_human(case.petitioner_name)
    if not decedent_human:
        return existing

    if ancestry_enricher.is_circuit_broken() or not ancestry_enricher._can_load_page():
        existing.notes = (existing.notes or "") + " [ancestry: circuit broken / daily limit]"
        return existing

    # Hint city from the decedent's tax-roll situs city (we typically have it
    # at this point because the ZIP gate already passed). State is always AL
    # for Benchmark cases.
    city_hint = (existing.decedent_city or "").strip()
    try:
        result = await ancestry_enricher.lookup_deceased(
            page, name=decedent_human, city=city_hint, state="AL",
        )
    except Exception as e:
        logger.warning("Ancestry lookup_deceased failed for %s: %s", decedent_human, e)
        existing.notes = (existing.notes or "") + f" [ancestry: error {type(e).__name__}]"
        return existing

    if not result or not result.get("confirmed_deceased"):
        existing.notes = (existing.notes or "") + " [ancestry: no match]"
        return existing

    # DOD sanity check: reject matches whose death predates the probate
    # filing by >3 years. SSDI especially is prone to name-collision
    # false positives (e.g. matching a different William Belew who died
    # in 2008 to a 2026 probate filing).
    candidate_dod = (result.get("date_of_death") or "").strip()
    if not _dod_within_window(
        candidate_dod, case.file_date, MAX_DOD_GAP_YEARS_FOR_FALLBACK,
    ):
        logger.info(
            "Ancestry match REJECTED for %s: DOD %s vs file date %s "
            "exceeds %d-year window (likely wrong person)",
            decedent_human, candidate_dod, case.file_date,
            MAX_DOD_GAP_YEARS_FOR_FALLBACK,
        )
        existing.notes = (
            (existing.notes or "")
            + f" [ancestry: rejected (DOD {candidate_dod} > "
            f"{MAX_DOD_GAP_YEARS_FOR_FALLBACK}y before filing)]"
        )
        return existing

    source_url = result.get("source_url") or ""
    source_type = result.get("source_type") or "ancestry"

    # Get obituary text. Newspapers tier returns a snippet directly; the
    # other tiers only return a URL we have to fetch.
    obit_text = result.get("obituary_text") or ""
    if not obit_text and source_url:
        obit_text = _fetch_page_text(source_url)

    if not obit_text or len(obit_text) < 200:
        # We confirmed the decedent is dead and got a URL, but couldn't pull
        # the text — record the source but can't extract survivors.
        existing.obituary_url = source_url
        if not existing.decedent_dod:
            existing.decedent_dod = result.get("date_of_death", "") or ""
        if not existing.decedent_full_name:
            existing.decedent_full_name = result.get("full_name", "") or ""
        existing.notes = (
            (existing.notes or "")
            + f" [ancestry: confirmed via {source_type} but obituary text unavailable]"
        )
        return existing

    # Run the SAME expanded LLM extraction the DDG path uses
    parsed = _parse_obituary_for_petitioner(
        obit_text, decedent_human, petitioner_human, api_key,
        rate_tracker=rate_tracker,
    )
    if not parsed:
        existing.notes = (
            (existing.notes or "")
            + f" [ancestry: {source_type} obituary text didn't validate to decedent]"
        )
        return existing

    # If we get here, Ancestry recovered an obituary that validates AND we
    # can parse. Replace the DDG result wholesale (it was 'none' anyway).
    existing.obituary_url = source_url

    existing.decedent_full_name = (parsed.get("decedent_full_name") or "").strip()
    existing.decedent_city = (parsed.get("decedent_city") or "").strip()
    existing.decedent_state = (parsed.get("decedent_state") or "").strip()
    try:
        existing.decedent_age_at_death = int(parsed.get("decedent_age_at_death") or 0)
    except (TypeError, ValueError):
        existing.decedent_age_at_death = 0
    existing.decedent_dod = (parsed.get("date_of_death") or "").strip()
    existing.decedent_obit_address = (parsed.get("decedent_obit_address") or "").strip()

    existing.petitioner_match = (parsed.get("petitioner_match") or "not_found").lower()
    existing.petitioner_survivor_name = (parsed.get("petitioner_survivor_name") or "").strip()
    existing.petitioner_relationship = (parsed.get("petitioner_relationship") or "").strip()
    existing.petitioner_city = (parsed.get("petitioner_city") or "").strip()

    survivors = parsed.get("all_survivors") or []
    survivors = [s for s in survivors if isinstance(s, dict) and s.get("name")]
    existing.all_survivors = survivors
    existing.spouse_name = (parsed.get("spouse_name") or "").strip()
    pid = parsed.get("preceded_in_death") or []
    existing.preceded_in_death = [str(n).strip() for n in pid if str(n).strip()]
    existing.executor_named = (parsed.get("executor_named") or "").strip()
    existing.heir_count = len(survivors)

    try:
        existing.decision_makers = rank_decision_makers(
            survivors=survivors,
            executor_name=existing.executor_named,
            decedent_name=existing.decedent_full_name or existing.decedent_name,
        )
    except Exception as e:
        logger.debug("rank_decision_makers failed in ancestry path: %s", e)
        existing.decision_makers = []

    existing.confidence = _grade_confidence(parsed)
    existing.notes = (existing.notes or "") + f" [ancestry: recovered via {source_type}]"
    return existing


async def batch_ancestry_fallback(
    cases_and_results: list[tuple[BenchmarkCase, MatchResult]],
    api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> int:
    """Run a single Ancestry browser session and try to upgrade every
    confidence=none MatchResult.

    Mutates the MatchResults in-place. Returns the number of upgrades.
    Safe to call with an empty list (fast no-op).
    """
    targets = [(c, r) for c, r in cases_and_results if r.confidence == "none"]
    if not targets:
        return 0

    import config as cfg
    if not (cfg.ANCESTRY_EMAIL and cfg.ANCESTRY_PASSWORD):
        logger.warning("Ancestry credentials missing — skipping fallback (%d candidates)",
                       len(targets))
        return 0

    import ancestry_enricher

    upgrades = 0
    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        logger.warning("Ancestry: could not launch browser — skipping fallback")
        return 0
    try:
        logger.info("Ancestry fallback: trying %d candidate(s)", len(targets))
        for case, result in targets:
            if ancestry_enricher.is_circuit_broken() or not ancestry_enricher._can_load_page():
                logger.warning("Ancestry: circuit/limit reached — stopping fallback")
                break
            before_conf = result.confidence
            await enrich_via_ancestry(
                case, result, page, api_key=api_key,
                rate_tracker=rate_tracker,
            )
            if result.confidence != before_conf:
                upgrades += 1
                logger.info("  Ancestry upgrade: %s now %s", case.case_number, result.confidence)
    finally:
        await ancestry_enricher.close_browser(pw, context)

    logger.info("Ancestry fallback complete: %d/%d upgraded", upgrades, len(targets))
    return upgrades


# ── CLI ────────────────────────────────────────────────────────────────


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Match a Benchmark Web petitioner against the decedent's obituary survivors.",
    )
    p.add_argument("--days-back", type=int, default=7,
                   help="How many days back to scan Benchmark for cases (default: 7)")
    p.add_argument("--limit", type=int, default=3,
                   help="Max cases to process (default: 3)")
    p.add_argument("--headed", action="store_true",
                   help="Run Benchmark browser in headed mode (visible)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    import asyncio
    from datetime import date, timedelta

    from benchmark_web import BenchmarkSession

    async def _run() -> list[MatchResult]:
        end = date.today()
        start = end - timedelta(days=args.days_back)
        results: list[MatchResult] = []
        async with BenchmarkSession(headless=not args.headed) as bm:
            cases = await bm.list_cases_in_date_range(start, end)
            logger.info("Pulled %d Benchmark cases; processing first %d",
                        len(cases), min(args.limit, len(cases)))
            for case in cases[: args.limit]:
                # Only fetch detail if convenience props aren't populated yet
                if not case.parties:
                    case = await bm.fetch_case_detail(case.case_url, case.case_number)
                if not case.decedent_name or not case.petitioner_name:
                    logger.info("  Skipping %s — missing decedent or petitioner",
                                case.case_number)
                    continue
                logger.info("→ %s  decedent=%r  petitioner=%r",
                            case.case_number, case.decedent_name, case.petitioner_name)
                r = match_petitioner_city(case)
                results.append(r)
        return results

    results = asyncio.run(_run())

    print(f"\nProcessed {len(results)} case(s)\n")
    for r in results:
        age = f", age {r.decedent_age_at_death}" if r.decedent_age_at_death else ""
        dod = f", dod={r.decedent_dod}" if r.decedent_dod else ""
        print(f"  {r.case_number}  confidence={r.confidence}  match={r.petitioner_match}")
        print(f"    decedent:    {r.decedent_name}  (city={r.decedent_city or '—'}{age}{dod})")
        if r.decedent_obit_address:
            print(f"    obit-addr:   {r.decedent_obit_address}")
        print(f"    petitioner:  {r.petitioner_name}")
        print(f"    survivor:    {r.petitioner_survivor_name or '—'}  "
              f"({r.petitioner_relationship or '?'})  city={r.petitioner_city or '—'}")
        if r.spouse_name:
            print(f"    spouse:      {r.spouse_name}")
        if r.executor_named:
            print(f"    executor:    {r.executor_named}  (named in obituary)")
        if r.heir_count:
            print(f"    heir count:  {r.heir_count}")
            for s in r.all_survivors[:6]:
                rel = s.get("relationship", "?")
                city = s.get("city", "")
                print(f"      · {s.get('name', '?')}  ({rel})  {city}")
        if r.preceded_in_death:
            print(f"    predeceased: {', '.join(r.preceded_in_death[:5])}")
        if r.decision_makers:
            print(f"    DM chain:")
            for dm in r.decision_makers[:3]:
                print(f"      [{dm.get('rank', '?')}] {dm.get('name', '?')}  "
                      f"({dm.get('relationship', '?')})  "
                      f"status={dm.get('status', '?')}  "
                      f"sign={dm.get('signing_authority', '?')}")
        print(f"    obituaries:  {r.obituaries_tried} tried, picked: {r.obituary_url or 'none'}")
        if r.notes:
            print(f"    notes:       {r.notes}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
