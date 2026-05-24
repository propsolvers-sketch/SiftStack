"""Jefferson County, AL post-probate pipeline orchestrator.

Pulls live cases from Benchmark Web, identifies the decedent's property via
the Jefferson E-Ring API, gates on Tier 1 / Tier 2 ZIP codes (target_zips),
and only spends LLM/skip-trace budget on cases whose property is in our
priority ZIPs.

Pipeline stages:

    1. Pull Benchmark cases for the date window
    2. For each case: search Jefferson property API by decedent's name
    3. Pick the primary parcel (homestead > highest-value)
    4. ZIP gate: keep only parcels in Tier 1 ∪ Tier 2 (configurable)
    5. For surviving cases: detect fiduciary petitioners (frequency count)
    6. For non-fiduciary survivors: cross-reference petitioner against the
       decedent's obituary survivors to recover petitioner's city
    7. Emit a structured result list + a per-stage summary

Output: list of CaseResult dicts. Wire to data_formatter / DataSift CSV
in the next step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import datasift_formatter
import jefferson_property_api as jpa
from benchmark_obituary_match import (
    MatchResult,
    _benchmark_name_to_human,
    batch_ancestry_fallback,
    match_petitioner_city,
)
from benchmark_web import BenchmarkCase, BenchmarkSession
from notice_parser import NoticeData, _split_decedent_name, _split_owner_name
from observability import (
    FunnelCounter,
    ServiceRateTracker,
    load_rolling_rates,
    rolling_rates_summary,
    save_rolling_rates,
)
from probate_property_locator import _search_jefferson, _score
from slack_notifier import (
    _send_blocks_webhook,
    build_funnel_block,
    build_service_rates_block,
)
from target_zips import zip_tier_county

load_dotenv()

logger = logging.getLogger(__name__)


# ── Phase 2: benchmark pipeline gate sequence (D-01 additive scope) ──
# Plan-checker W8: benchmark is NOT in CONTEXT.md D-01's 5-pipeline
# list; it's documented in 02-04 must_haves so the addition stays
# auditable. The 6-gate sequence mirrors the benchmark stage order
# (Benchmark Web → property → tier → fiduciary detection → obituary →
# Tracerfy → DataSift CSV).
BENCHMARK_GATES: tuple[str, ...] = (
    "pulled",
    "tier_gated",
    "fiduciary_filtered",
    "obituary_confirmed",
    "tracerfy_matched",
    "datasift_uploaded",
)


# ── Result schema ─────────────────────────────────────────────────────


@dataclass
class CaseResult:
    """End-to-end outcome for one Benchmark case."""

    case_number: str
    case_url: str
    case_type: str
    file_date: str
    decedent_name: str        # Human-readable (First Middle Last)
    petitioner_name: str
    attorney_name: str

    # Property API stage
    property_lookup_attempted: bool = False
    property_found: bool = False
    parcel_id: str = ""
    situs_address: str = ""
    situs_city: str = ""
    situs_zip: str = ""
    is_homestead: bool = False
    total_value: float = 0.0
    is_delinquent: bool = False
    municipality: str = ""

    # Tier gate
    tier: Optional[int] = None       # 1, 2, or None
    in_target_zip: bool = False

    # Petitioner classification
    petitioner_is_fiduciary: bool = False
    petitioner_case_count: int = 1   # How many times this petitioner appears in this batch

    # Obituary cross-reference (only run for in-tier, non-fiduciary cases)
    obituary_run: bool = False
    obituary_match: Optional[MatchResult] = None

    # Disposition
    status: str = "unknown"  # one of: enriched, dropped_off_target, dropped_no_property,
                             # skipped_fiduciary, error
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.obituary_match is not None:
            d["obituary_match"] = self.obituary_match.to_dict()
        return d


# ── Property selection ────────────────────────────────────────────────


def _benchmark_decedent_to_query(raw: str) -> str:
    """Convert Benchmark "LAST, FIRST MIDDLE" → "LAST FIRST MIDDLE" for the
    Jefferson API (which expects last-first ordering with no comma).
    """
    if not raw:
        return ""
    cleaned = raw.replace(",", " ").strip()
    return " ".join(cleaned.split())


# ── Pipeline ──────────────────────────────────────────────────────────


def _attach_property(
    case: BenchmarkCase, decedent_human: str, min_score: float = 0.5,
) -> tuple[Optional[jpa.JeffersonPropertyRecord], list[jpa.JeffersonPropertyRecord]]:
    """Run Jefferson property search for one case. Returns (primary, scored_matches).

    Uses probate_property_locator._search_jefferson which handles last-first
    reordering AND middle-name truncation fallbacks. Then scores each result
    by token overlap against the full decedent name and picks the best match
    that clears ``min_score``. Homestead-flagged matches are preferred when
    scores tie.
    """
    query = _benchmark_decedent_to_query(case.decedent_name)
    if not query:
        return (None, [])
    try:
        records = _search_jefferson(query)
    except Exception as e:
        logger.warning("Property API failed for %s: %s", decedent_human, e)
        return (None, [])
    if not records:
        return (None, [])

    scored = [(r, _score(query, r.owner_name)) for r in records]
    scored = [(r, s) for r, s in scored if s >= min_score]
    if not scored:
        return (None, records)

    # Best match: homestead first (within tied scores), then highest score,
    # then highest total_value as final tiebreaker.
    scored.sort(
        key=lambda rs: (rs[0].is_homestead, rs[1], rs[0].total_value),
        reverse=True,
    )
    return (scored[0][0], [r for r, _ in scored])


def _classify_fiduciaries(cases: list[BenchmarkCase]) -> dict[str, int]:
    """Return {petitioner_name_normalized: count} across the batch.
    Names appearing 2+ times are treated as professional fiduciaries.
    """
    names = []
    for c in cases:
        n = _benchmark_name_to_human(c.petitioner_name).strip().lower()
        if n:
            names.append(n)
    return dict(Counter(names))


async def run_pipeline(
    days_back: int = 7,
    max_cases: int = 50,
    tier_filter: tuple[int, ...] = (1, 2),
    enable_obituary: bool = True,
    enable_ancestry_fallback: bool = False,
    headless: bool = True,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[CaseResult], FunnelCounter, ServiceRateTracker]:
    """End-to-end pipeline: Benchmark → property API → ZIP gate → obituary.

    Args:
        days_back: Look back this many days from today on Benchmark.
        max_cases: Cap the number of cases processed (defensive).
        tier_filter: Which ZIP tiers to keep. (1, 2) = Tier 1 ∪ Tier 2.
                     (1,) = Tier 1 only. () = no filter (run everything).
        enable_obituary: If False, skip the obituary cross-reference step.
        enable_ancestry_fallback: If True, run a batch Ancestry/Newspapers.com
            pass to upgrade any in-tier cases that DDG returned as confidence=none.
            Costs ancestry page-loads (100/day cap) and adds ~30s per fallback.
        headless: Pass-through to BenchmarkSession.
        funnel: Phase 2 — optional caller-supplied counter. When omitted, a
            fresh FunnelCounter("benchmark") is created with the 6-gate
            sequence pre-seeded so the Slack block always renders all gates.
        rate_tracker: Phase 2 — optional caller-supplied tracker. When omitted,
            a fresh ServiceRateTracker is created. Threaded into the LLM
            obituary-match call sites + batch_skip_trace so the 4-service
            rates aggregate across the run.

    Returns:
        ``(results, funnel, rate_tracker)`` — per-case results plus the
        populated funnel + tracker so notify_slack can append the funnel
        + service-rates blocks to the run summary (D-02).
    """
    if funnel is None:
        funnel = FunnelCounter("benchmark", gates=list(BENCHMARK_GATES))
    if rate_tracker is None:
        rate_tracker = ServiceRateTracker()

    end = date.today()
    start = end - timedelta(days=days_back)
    results: list[CaseResult] = []

    async with BenchmarkSession(headless=headless) as bm:
        cases = await bm.list_cases_in_date_range(start, end)
        cases = cases[:max_cases]
        logger.info("Pulled %d Benchmark case(s) in window %s..%s",
                    len(cases), start, end)

        # Hydrate each case's detail (parties / accordions)
        hydrated: list[BenchmarkCase] = []
        for c in cases:
            if not c.parties:
                c = await bm.fetch_case_detail(c.case_url, c.case_number)
            hydrated.append(c)

    # Phase 2 funnel: `pulled` gate — count of cases returned by
    # Benchmark Web (post any internal trim, pre property-locator).
    funnel.set("pulled", len(hydrated))

    case_by_num: dict[str, BenchmarkCase] = {c.case_number: c for c in hydrated}

    # Pre-compute fiduciary detection across the whole batch
    petitioner_counts = _classify_fiduciaries(hydrated)
    fiduciary_threshold = 2

    for case in hydrated:
        decedent_human = _benchmark_name_to_human(case.decedent_name)
        petitioner_human = _benchmark_name_to_human(case.petitioner_name)
        petitioner_norm = petitioner_human.strip().lower()
        case_count = petitioner_counts.get(petitioner_norm, 1)
        is_fiduciary = case_count >= fiduciary_threshold

        result = CaseResult(
            case_number=case.case_number,
            case_url=case.case_url,
            case_type=case.case_type,
            file_date=case.file_date,
            decedent_name=decedent_human,
            petitioner_name=petitioner_human,
            attorney_name=_benchmark_name_to_human(case.attorney_name),
            petitioner_is_fiduciary=is_fiduciary,
            petitioner_case_count=case_count,
        )

        if not decedent_human:
            result.status = "error"
            result.notes = "No decedent name from Benchmark"
            results.append(result)
            continue

        # Stage 1: Property lookup
        result.property_lookup_attempted = True
        primary, all_records = _attach_property(case, decedent_human)
        if primary is None:
            result.status = "dropped_no_property"
            result.notes = f"No Jefferson parcel found for decedent '{decedent_human}'"
            logger.info("  DROP %s (no property): %s", case.case_number, decedent_human)
            results.append(result)
            continue

        result.property_found = True
        result.parcel_id = primary.parcel_number
        result.situs_address = primary.situs_address
        result.situs_city = primary.situs_city
        result.situs_zip = primary.situs_zip
        result.is_homestead = primary.is_homestead
        result.total_value = primary.total_value
        result.is_delinquent = primary.is_delinquent
        result.municipality = primary.municipality

        # Stage 2: ZIP tier gate
        tier, _county = zip_tier_county(primary.situs_zip)
        result.tier = tier

        if tier_filter and (tier is None or tier not in tier_filter):
            result.status = "dropped_off_target"
            result.notes = (
                f"ZIP {primary.situs_zip or '(empty)'} not in tier filter {tier_filter}"
            )
            logger.info("  DROP %s (tier=%s, zip=%s): %s",
                        case.case_number, tier, primary.situs_zip, primary.situs_address)
            results.append(result)
            continue

        result.in_target_zip = True
        logger.info("  KEEP %s (tier=%s, zip=%s): %s",
                    case.case_number, tier, primary.situs_zip, primary.situs_address)

        # Stage 3: Skip obituary if petitioner is a fiduciary
        if is_fiduciary:
            result.status = "skipped_fiduciary"
            result.notes = (
                f"Petitioner '{petitioner_human}' appears in {case_count} cases this batch — "
                f"likely attorney/public administrator. Skipping obituary."
            )
            logger.info("  SKIP-OBIT %s: fiduciary petitioner %s (%d cases)",
                        case.case_number, petitioner_human, case_count)
            results.append(result)
            continue

        # Stage 4: Obituary cross-reference
        if enable_obituary:
            try:
                m = match_petitioner_city(case, rate_tracker=rate_tracker)
                result.obituary_match = m
                result.obituary_run = True
            except Exception as e:
                logger.warning("Obituary match failed for %s: %s", case.case_number, e)
                result.notes = f"Obituary match error: {e}"

        result.status = "enriched"
        results.append(result)

    # ── Stage 5: Ancestry fallback for confidence=none cases ─────────
    if enable_obituary and enable_ancestry_fallback:
        targets = [
            (case_by_num[r.case_number], r.obituary_match)
            for r in results
            if r.status == "enriched"
            and r.obituary_match is not None
            and r.obituary_match.confidence == "none"
            if r.case_number in case_by_num
        ]
        if targets:
            logger.info("Triggering Ancestry batch fallback for %d case(s)", len(targets))
            try:
                upgrades = await batch_ancestry_fallback(
                    targets, rate_tracker=rate_tracker,
                )
                logger.info("Ancestry fallback: %d upgrade(s)", upgrades)
            except Exception as e:
                logger.warning("Ancestry batch failed: %s", e)

    # ── Phase 2 funnel: stamp the disposition-derived gates ──────────
    # tier_gated: cases that survived the Tier 1/2 ZIP filter.
    # fiduciary_filtered: tier-surviving cases whose petitioner is NOT
    #   a fiduciary (i.e. these proceed to obituary spend). Equal to
    #   enriched + skipped_fiduciary-inverse → count of results with
    #   status == "enriched" (since fiduciaries route to
    #   "skipped_fiduciary" before the obit step).
    # obituary_confirmed: count of enriched results whose obit match
    #   landed at confidence in {high, medium}.
    in_tier_count = sum(
        1 for r in results
        if r.status in ("enriched", "skipped_fiduciary")
    )
    funnel.set("tier_gated", in_tier_count)

    enriched_count = sum(1 for r in results if r.status == "enriched")
    funnel.set("fiduciary_filtered", enriched_count)

    obit_confirmed_count = sum(
        1 for r in results
        if r.status == "enriched"
        and r.obituary_match is not None
        and r.obituary_match.confidence in ("high", "medium")
    )
    funnel.set("obituary_confirmed", obit_confirmed_count)

    return (results, funnel, rate_tracker)


# ── DataSift CSV conversion ──────────────────────────────────────────


def _granted_to_creditor_deadline(granted: str) -> str:
    """granted_date + 6 months (AL § 43-2-350) → YYYY-MM-DD. Empty input → empty."""
    g = (granted or "").strip()
    if not g:
        return ""
    try:
        d = datetime.strptime(g, "%Y-%m-%d")
    except ValueError:
        return ""
    # 6 months later
    month = d.month + 6
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    try:
        return d.replace(year=year, month=month).strftime("%Y-%m-%d")
    except ValueError:
        # day-of-month overflow (e.g. Aug 31 + 6mo → Feb 29 in non-leap year)
        # → step back to the last valid day of the target month
        for day_offset in range(1, 4):
            try:
                return d.replace(year=year, month=month, day=d.day - day_offset).strftime(
                    "%Y-%m-%d",
                )
            except ValueError:
                continue
    return ""


def _to_notice_data(r: CaseResult) -> NoticeData:
    """Convert an enriched CaseResult into a NoticeData ready for the DataSift formatter.

    Only intended for results with status == "enriched"; the formatter will
    treat anything missing as a blank field.
    """
    notice = NoticeData()

    # ── Notice metadata ──
    notice.notice_type = "probate"
    notice.notice_subtype = "probate_creditors"  # Benchmark cases are post-Letters
    notice.county = "Jefferson"
    notice.state = "AL"
    notice.received_date = date.today().strftime("%Y-%m-%d")
    notice.date_added = r.file_date or notice.received_date
    notice.case_number = r.case_number
    notice.source_url = r.case_url

    # ── Property (situs) ──
    notice.address = r.situs_address
    notice.city = r.situs_city
    notice.zip = r.situs_zip
    notice.parcel_id = r.parcel_id
    notice.is_homestead = "Y" if r.is_homestead else ""
    notice.assessed_value = f"{r.total_value:.0f}" if r.total_value else ""
    notice.municipality = r.municipality

    # ── Decedent (split into first/middle/last/suffix) ──
    notice.decedent_name = r.decedent_name
    _split_decedent_name(notice)

    # ── Owner (= the Personal Representative / petitioner) ──
    # Mailing address is not provided by Benchmark; Smarty downstream may fill it.
    notice.owner_name = r.petitioner_name
    _split_owner_name(notice)

    # ── Obituary-derived fields ──
    obit = r.obituary_match
    if obit and obit.confidence != "none":
        notice.owner_deceased = "yes"  # decedent is the property owner of record
        notice.date_of_death = obit.decedent_dod or ""
        notice.obituary_url = obit.obituary_url or ""
        notice.dm_confidence = obit.confidence
        notice.obituary_source_type = "full_page"

        # Decision-maker: prefer the ranked DM chain (spouse > children > siblings)
        # over the petitioner's role, since the petitioner may just be the
        # paperwork-filer (e.g. Cindy G Smith filed for her brother but Dede
        # Ferebee, the surviving spouse, is the actual DM).
        dms = obit.decision_makers or []
        if dms:
            primary = dms[0]
            notice.decision_maker_name = primary.get("name", "")
            notice.decision_maker_relationship = primary.get("relationship", "")
            notice.decision_maker_status = primary.get("status", "unverified")
            notice.decision_maker_source = primary.get("source", "obituary_survivors")

            # Counts for DataSift "Heir Count" / "Heirs Living" columns
            living = sum(1 for d in dms if d.get("status") == "verified_living")
            unverified = sum(1 for d in dms if d.get("status") == "unverified")
            deceased = sum(1 for d in dms if d.get("status") == "verified_deceased")
            notice.heirs_verified_living = str(living) if living else ""
            notice.heirs_unverified = str(unverified) if unverified else ""
            notice.heirs_verified_deceased = str(deceased) if deceased else ""
            notice.heir_map_json = json.dumps(dms)

            # Signing chain — DMs whose AL law gives them signing authority
            signers = [d for d in dms if d.get("signing_authority")]
            notice.signing_chain_count = str(len(signers)) if signers else ""
            notice.signing_chain_names = ", ".join(
                d.get("name", "") for d in signers if d.get("name")
            )

        # If the petitioner-as-survivor lookup gave us a city, attach it as
        # the DM city (will be used by Smarty downstream for mailing addr lookup).
        if obit.petitioner_city and notice.decision_maker_name == _benchmark_name_to_human(
            r.petitioner_name
        ):
            notice.decision_maker_city = obit.petitioner_city
            notice.decision_maker_state = obit.decedent_state or "AL"
    else:
        # No obituary match — the property is still a clean lead, just without
        # the heir tree. Fall back to the petitioner: they're the
        # court-appointed Personal Representative per the Benchmark filing,
        # so they ARE a valid decision-maker even without obituary corroboration.
        notice.owner_deceased = "yes"
        notice.decision_maker_name = r.petitioner_name
        notice.decision_maker_relationship = "personal_representative"
        notice.decision_maker_status = "court_appointed"
        notice.decision_maker_source = "benchmark_court_record"
        notice.dm_confidence = "medium"  # court-appointed is fundamentally trustworthy
        notice.dm_confidence_reason = (
            "no_obituary_found_using_court_pr" if not obit
            else "obituary_petitioner_not_in_survivors_using_court_pr"
        )
        notice.missing_data_flags = "no_obituary_match"

    # ── AL probate enrichment fields (for DataSift custom columns) ──
    # granted_date is the Benchmark file_date for our purposes — Letters are
    # typically issued the same day or within a couple days of filing.
    notice.granted_date = r.file_date or ""
    notice.creditor_deadline = _granted_to_creditor_deadline(notice.granted_date)

    return notice


def build_slack_message(
    results: list[CaseResult],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
    days_back: int = 7,
) -> str:
    """Build a concise post-probate Slack/Discord notification.

    Format (one message):
      header → counts by disposition → per-enriched-case action card →
      drops summary → cost line.

    Plain text with Slack mrkdwn (asterisks for bold). Discord renders
    most of the same syntax via the /slack compatibility endpoint.
    """
    total = len(results)
    by_status = Counter(r.status for r in results)
    enriched = [r for r in results if r.status == "enriched"]
    by_tier = Counter(r.tier for r in enriched)

    lines: list[str] = []
    today = date.today().strftime("%Y-%m-%d")
    lines.append(f"*Jefferson Post-Probate — {today}* (last {days_back}d)")
    lines.append(
        f"  scraped: {total}  ·  in-tier: {len(enriched)} "
        f"(T1: {by_tier.get(1, 0)}  T2: {by_tier.get(2, 0)})  ·  "
        f"off-tier: {by_status.get('dropped_off_target', 0)}  ·  "
        f"no-property: {by_status.get('dropped_no_property', 0)}  ·  "
        f"fiduciary: {by_status.get('skipped_fiduciary', 0)}"
    )

    if not enriched:
        lines.append("")
        lines.append("_No new in-tier post-probate leads this window._")
        return "\n".join(lines)

    # Per-case action cards
    lines.append("")
    lines.append(f"*New leads — {len(enriched)}*")

    for r in enriched:
        addr = r.situs_address or "(address unknown)"
        val = f"${r.total_value:,.0f}" if r.total_value else "$?"
        tier_label = f"T{r.tier}" if r.tier else "T?"
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
        lines.append(f"    case `{r.case_number}`  decedent: {r.decedent_name}")

        obit = r.obituary_match
        dm_line = ""
        confidence_line = ""
        contact_line = ""

        if obit and obit.confidence != "none":
            dm_name = obit.primary_dm_name or r.petitioner_name
            dm_rel = obit.primary_dm_relationship or "?"
            dm_line = f"    DM: *{dm_name}* ({dm_rel})"
            if obit.decedent_age_at_death:
                dm_line += f"  ·  decedent age {obit.decedent_age_at_death}"
            if obit.decedent_dod:
                dm_line += f", DoD {obit.decedent_dod}"
            confidence_line = f"    obit confidence: {obit.confidence}"
            if obit.heir_count:
                confidence_line += f"  ·  {obit.heir_count} heirs"
            if obit.spouse_name:
                confidence_line += f"  ·  spouse: {obit.spouse_name}"
            if obit.obituary_url:
                confidence_line += f"  ·  <{obit.obituary_url}|obit>"
        else:
            # PR-fallback path
            dm_line = (
                f"    DM: *{r.petitioner_name}* (court-appointed PR — no obituary match)"
            )
            confidence_line = "    obit confidence: none — manual research recommended"

        lines.append(dm_line)
        lines.append(confidence_line)

        # Contact info: pull from the converted NoticeData if available, but
        # since we only have CaseResult here, surface what we know directly.
        # The phone/address lives on the CSV row; we summarize next.

    # Skip-trace stats (if run)
    if skip_trace_stats:
        sub = skip_trace_stats.get("submitted", 0)
        matched = skip_trace_stats.get("matched", 0)
        ph = skip_trace_stats.get("phones_found", 0)
        em = skip_trace_stats.get("emails_found", 0)
        cost = skip_trace_stats.get("cost", 0.0)
        if sub:
            lines.append("")
            lines.append(
                f"*Skip-trace:* {matched}/{sub} contacts matched  ·  "
                f"{ph} phones, {em} emails  ·  ${cost:.2f}"
            )

    # CSV path
    if csv_path:
        lines.append("")
        lines.append(f"*CSV:* `{csv_path.name}` — {csv_path.parent}")

    return "\n".join(lines)


def notify_slack(
    results: list[CaseResult],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
    days_back: int = 7,
    webhook_url: str | None = None,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> bool:
    """Post the benchmark run summary to Slack/Discord. Returns True on success.

    Phase 2 (OPS-03 / OBS-01): when ``funnel`` AND ``rate_tracker`` are
    BOTH supplied (the new daily-ops path), this function:

      1. Calls load_rolling_rates BEFORE the blocks build so today's
         service-rates block shows the PRIOR-days baseline (D-03).
      2. Builds a 3-block payload: legacy summary text + funnel block +
         service-rates block.
      3. POSTs via _send_blocks_webhook — one HTTP call, one message
         (D-02).
      4. AFTER a successful send, calls save_rolling_rates so today's
         totals advance the window for tomorrow's baseline (D-03 / W6).

    Legacy callers (no funnel + no tracker) get the byte-identical
    plain-text path via _send_webhook (W5 preserved).
    """
    import slack_notifier
    text = build_slack_message(
        results, csv_path=csv_path,
        skip_trace_stats=skip_trace_stats, days_back=days_back,
    )

    # Legacy path: no funnel + no tracker → plain text via _send_webhook,
    # byte-identical to the pre-Phase-2 behaviour.
    if funnel is None and rate_tracker is None:
        sent = slack_notifier._send_webhook(text, webhook_url=webhook_url)
        if sent:
            logger.info("Slack notification sent (%d enriched leads)",
                        sum(1 for r in results if r.status == "enriched"))
        else:
            logger.warning("Slack notification failed (no webhook or send error)")
        return sent

    # Phase 2 path: rolling-rates ordering (D-03) — load BEFORE the
    # blocks build, save AFTER a successful send.
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
        # W6: save ONLY after a successful send so a failed Slack post
        # leaves the rolling baseline untouched.
        save_rolling_rates(rate_tracker.totals())

    if sent:
        logger.info("Slack notification sent (%d enriched leads)",
                    sum(1 for r in results if r.status == "enriched"))
    else:
        logger.warning("Slack notification failed (no webhook or send error)")
    return sent


def _promote_heir_contacts_to_csv_slots(notice: NoticeData) -> None:
    """Fill empty Phone/Email slots on the NoticeData from heir_map_json.

    Tracerfy's batch trace stores phones/emails for non-DM-#1 heirs inside
    each heir's entry in heir_map_json. The DataSift formatter, however,
    populates Phone 1-9 / Email 1-5 from flat NoticeData fields
    (primary_phone, mobile_1-5, landline_1-3, email_1-5). Without this
    promotion pass, heir phones never appear in the CSV columns DataSift
    filter presets read.

    Walks heirs in their stored rank order and fills empty slots in the
    same order, deduping on phone/email value.
    """
    if not notice.heir_map_json:
        return
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return

    # Slots to fill, in order of preference. (attr_name, value)
    phone_slots = [
        "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5",
        "landline_1", "landline_2", "landline_3",
    ]
    email_slots = ["email_1", "email_2", "email_3", "email_4", "email_5"]

    seen_phones = {getattr(notice, s) for s in phone_slots if getattr(notice, s)}
    seen_emails = {getattr(notice, s) for s in email_slots if getattr(notice, s)}

    def _next_empty(slots: list[str]) -> Optional[str]:
        for s in slots:
            if not getattr(notice, s):
                return s
        return None

    for heir in heirs:
        for ph in (heir.get("phones") or []):
            ph = (ph or "").strip()
            if not ph or ph in seen_phones:
                continue
            slot = _next_empty(phone_slots)
            if not slot:
                break
            setattr(notice, slot, ph)
            seen_phones.add(ph)
        for em in (heir.get("emails") or []):
            em = (em or "").strip()
            if not em or em in seen_emails:
                continue
            slot = _next_empty(email_slots)
            if not slot:
                break
            setattr(notice, slot, em)
            seen_emails.add(em)


def prepare_notices(
    results: list[CaseResult],
    enriched_only: bool = True,
    skip_trace: bool = False,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[NoticeData], dict | None]:
    """Convert pipeline results to NoticeData and optionally run skip-trace.

    Returns (notices, skip_trace_stats). ``skip_trace_stats`` is None when
    skip-trace wasn't requested.

    Phase 2: when ``funnel`` is supplied, sets ``tracerfy_matched`` from
    skip-trace stats. When ``rate_tracker`` is supplied, it's threaded
    into ``batch_skip_trace`` so per-contact match/miss counts feed the
    per-run + 7-day Tracerfy rates.
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
            # Surface any heir-only phones into the DataSift Phone N slots.
            # Without this, phones found for signing-authority heirs (when
            # DM #1 didn't match) live only in heir_map_json and never appear
            # in the CSV columns DataSift filter presets read.
            for n in notices:
                _promote_heir_contacts_to_csv_slots(n)
            if funnel is not None:
                funnel.set("tracerfy_matched", int(stats.get("matched", 0)))
        except Exception as e:
            logger.warning("Skip-trace failed (continuing without phones): %s", e)
            stats = {"error": str(e)}
            # Zero-fill the gate so it always appears in the Slack block.
            if funnel is not None:
                funnel.set("tracerfy_matched", 0)
    elif funnel is not None:
        # skip-trace not requested (or no notices) → zero-fill so the
        # gate still appears in the Slack block.
        funnel.set("tracerfy_matched", 0)

    return notices, stats


def write_datasift_csv(
    results: list[CaseResult],
    filename: str | None = None,
    enriched_only: bool = True,
    skip_trace: bool = False,
) -> Optional[Path]:
    """Convert pipeline results to NoticeData and write a DataSift upload CSV.

    Convenience wrapper around ``prepare_notices`` + ``datasift_formatter.write_datasift_csv``.
    For callers that need both the CSV path AND the skip-trace stats (e.g.,
    Slack notification), use ``prepare_notices`` directly and call the
    formatter yourself.
    """
    notices, _stats = prepare_notices(
        results, enriched_only=enriched_only, skip_trace=skip_trace,
    )
    if not notices:
        logger.info("DataSift CSV: 0 eligible records — nothing to write.")
        return None
    return datasift_formatter.write_datasift_csv(notices, filename=filename)


# ── Reporting ─────────────────────────────────────────────────────────


def _print_summary(results: list[CaseResult]) -> None:
    """Print a per-stage summary of the pipeline run."""
    total = len(results)
    by_status = Counter(r.status for r in results)
    enriched = [r for r in results if r.status == "enriched"]
    by_tier = Counter(r.tier for r in enriched)

    print(f"\n{'═' * 64}")
    print(f"  Pipeline summary — {total} case(s) processed")
    print(f"{'═' * 64}")
    print(f"  enriched (in target ZIP):     {by_status['enriched']}")
    print(f"    Tier 1:                     {by_tier.get(1, 0)}")
    print(f"    Tier 2:                     {by_tier.get(2, 0)}")
    print(f"  dropped (off-target ZIP):     {by_status['dropped_off_target']}")
    print(f"  dropped (no property found):  {by_status['dropped_no_property']}")
    print(f"  skipped (fiduciary):          {by_status['skipped_fiduciary']}")
    print(f"  errors:                       {by_status['error']}")
    print()

    if enriched:
        print(f"  ━━━ Enriched cases ━━━")
        for r in enriched:
            print(f"  • {r.case_number}  T{r.tier}  zip={r.situs_zip}  "
                  f"{r.situs_address[:40]}  ${r.total_value:,.0f}"
                  f"{'  HOMESTEAD' if r.is_homestead else ''}"
                  f"{'  DELINQUENT' if r.is_delinquent else ''}")
            print(f"      decedent={r.decedent_name}  petitioner={r.petitioner_name}")
            obit = r.obituary_match
            if obit:
                age = f", age {obit.decedent_age_at_death}" if obit.decedent_age_at_death else ""
                dod = f", dod={obit.decedent_dod}" if obit.decedent_dod else ""
                print(f"      obit: confidence={obit.confidence}{age}{dod}")
                if obit.petitioner_match != "not_found":
                    pcity = f", city={obit.petitioner_city}" if obit.petitioner_city else ""
                    print(f"      petitioner is {obit.petitioner_relationship or '?'} "
                          f"({obit.petitioner_match}{pcity})")
                if obit.spouse_name:
                    print(f"      spouse: {obit.spouse_name}")
                if obit.executor_named:
                    print(f"      executor named: {obit.executor_named}")
                if obit.heir_count:
                    print(f"      heir count: {obit.heir_count}")
                if obit.decision_makers:
                    dm = obit.decision_makers[0]
                    print(f"      DM #1: {dm.get('name', '?')} "
                          f"({dm.get('relationship', '?')}, {dm.get('signing_authority', '?')})")
                if obit.preceded_in_death:
                    print(f"      predeceased: {', '.join(obit.preceded_in_death[:3])}")
                if obit.obituary_url:
                    print(f"      obit-url: {obit.obituary_url}")

    dropped_off = [r for r in results if r.status == "dropped_off_target"]
    if dropped_off:
        print(f"\n  ━━━ Off-target ZIPs (dropped) ━━━")
        for r in dropped_off:
            print(f"  · {r.case_number}  zip={r.situs_zip}  {r.situs_address[:40]}  "
                  f"({r.decedent_name})")

    dropped_no = [r for r in results if r.status == "dropped_no_property"]
    if dropped_no:
        print(f"\n  ━━━ No property found (dropped) ━━━")
        for r in dropped_no:
            print(f"  · {r.case_number}  ({r.decedent_name})")

    skipped = [r for r in results if r.status == "skipped_fiduciary"]
    if skipped:
        print(f"\n  ━━━ Fiduciary petitioners (obituary skipped) ━━━")
        for r in skipped:
            print(f"  · {r.case_number}  T{r.tier}  zip={r.situs_zip}  "
                  f"petitioner={r.petitioner_name} ({r.petitioner_case_count} cases)")
    print()


# ── CLI ───────────────────────────────────────────────────────────────


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Run the Jefferson AL post-probate pipeline (Benchmark → property → ZIP gate → obituary).",
    )
    p.add_argument("--days-back", type=int, default=7,
                   help="Days of Benchmark history to scan (default: 7)")
    p.add_argument("--max-cases", type=int, default=50,
                   help="Hard cap on cases processed (default: 50)")
    p.add_argument("--tiers", type=str, default="1,2",
                   help="Comma-separated tier filter: '1', '2', '1,2', or 'none' (default: 1,2)")
    p.add_argument("--no-obituary", action="store_true",
                   help="Skip obituary cross-reference step (fastest mode)")
    p.add_argument("--ancestry-fallback", action="store_true",
                   help="When DDG returns confidence=none, fall through to Ancestry + "
                        "Newspapers.com (heavy: launches headed Playwright, ~30s per fallback, "
                        "consumes ancestry page-load budget — daily cap 100).")
    p.add_argument("--datasift-csv", action="store_true",
                   help="Write enriched results to a DataSift-formatted upload CSV "
                        "in output/ (timestamped). Only enriched (in-tier) records are exported.")
    p.add_argument("--skip-trace", action="store_true",
                   help="With --datasift-csv: also run Tracerfy batch skip-trace on the DMs "
                        "and signing-authority heirs to populate Phone 1-9 / Email 1-5. "
                        "Costs ~$0.02 per contact. Requires TRACERFY_API_KEY.")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post a run summary to Slack/Discord (uses SLACK_WEBHOOK_URL from env). "
                        "Includes per-lead action cards for in-tier cases.")
    p.add_argument("--headed", action="store_true",
                   help="Run Benchmark browser in headed mode")
    p.add_argument("--json", action="store_true",
                   help="Output structured JSON instead of summary")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose logging")
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

    # Phase 2: run_pipeline now returns (results, funnel, rate_tracker).
    results, funnel, rate_tracker = asyncio.run(run_pipeline(
        days_back=args.days_back,
        max_cases=args.max_cases,
        tier_filter=tier_filter,
        enable_obituary=not args.no_obituary,
        enable_ancestry_fallback=args.ancestry_fallback,
        headless=not args.headed,
    ))

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        _print_summary(results)

    csv_path: Optional[Path] = None
    skip_trace_stats: dict | None = None

    if args.datasift_csv:
        notices, skip_trace_stats = prepare_notices(
            results, skip_trace=args.skip_trace,
            funnel=funnel, rate_tracker=rate_tracker,
        )
        if notices:
            csv_path = datasift_formatter.write_datasift_csv(notices)
            # Phase 2 funnel: datasift_uploaded gate stamps from the
            # surviving notice count after CSV write.
            funnel.set("datasift_uploaded", len(notices))
            print(f"\n✓ DataSift CSV written: {csv_path}")
        else:
            funnel.set("datasift_uploaded", 0)
            print("\n· DataSift CSV: 0 eligible records (no enriched cases).")
    elif args.skip_trace:
        print("\n· --skip-trace ignored (requires --datasift-csv).")

    # D-04 — terminal mirrors Slack: log the funnel at end-of-run
    # regardless of whether --notify-slack is set.
    logger.info(
        "Funnel (%s): %s",
        funnel.pipeline_name, dict(funnel.as_ordered_dict()),
    )

    if args.notify_slack:
        sent = notify_slack(
            results, csv_path=csv_path,
            skip_trace_stats=skip_trace_stats, days_back=args.days_back,
            funnel=funnel, rate_tracker=rate_tracker,
        )
        if sent:
            print(f"✓ Slack notification posted")
        else:
            print(f"· Slack notification failed (check SLACK_WEBHOOK_URL)")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
