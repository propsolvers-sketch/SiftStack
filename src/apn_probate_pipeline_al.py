"""APN-driven post-probate pipeline orchestrator (Jefferson + Madison).

Companion to:
  - benchmark_pipeline_al.py — Jefferson court-records (Benchmark Web)
  - pre_probate_pipeline_al.py — obituary-driven, days-fresh

Where Benchmark gives us live Jefferson probate cases directly from the
court system, this orchestrator pulls the same shape of post-probate data
from the alabamapublicnotices.com newspaper publications. **This is the
canonical Madison post-probate path** — Madison has no Benchmark equivalent
(its online portal is recording-only). Jefferson runs through both
Benchmark AND APN; the duplication is fine because Benchmark covers the
full case stream while APN covers only what gets formally published.

Pipeline stages:

    1. Scrape APN for Jefferson + Madison probate Notice-to-Creditors
    2. For each notice: enrich via probate_property_locator (county-routed)
    3. Madison-only: one-shot Smarty geocode to recover missing ZIP
    4. ZIP gate: keep only parcels in Tier 1 ∪ Tier 2
    5. Reuse pre-probate's _to_notice_data → CSV writer + Slack notification

Reuses the existing scraper, probate_property_locator, target_zips, and
the pre-probate DM-ranking + DataSift CSV path. The genuinely new piece
here is just the orchestrator wiring.

Note: APN scraping requires CAPTCHA_API_KEY (2Captcha) since
alabamapublicnotices.com gates every detail page behind reCAPTCHA v2.
Each notice costs ~$0.003 in CAPTCHA solves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import config as cfg
import datasift_formatter
from notice_parser import NoticeData
from observability import (
    FunnelCounter,
    ServiceRateTracker,
    load_rolling_rates,
    rolling_rates_summary,
    save_rolling_rates,
)
from pre_probate_pipeline_al import (
    _normalize_decedent_key,
    _promote_heir_contacts_to_csv_slots,
)
from address_standardizer import (
    smarty_zip_for_madison_address,
    smarty_zip_for_marshall_address,
    smarty_zip_or_city_estimate_for_madison,
    smarty_zip_or_city_estimate_for_marshall,
)
from probate_property_locator import enrich_notice_with_property
from scraper import scrape_all
from slack_notifier import (
    _send_blocks_webhook,
    build_funnel_block,
    build_service_rates_block,
)
from target_zips import zip_tier_county

load_dotenv(dotenv_path=Path.home() / "Desktop/SiftStack/.env")

logger = logging.getLogger(__name__)


# ── Result schema ─────────────────────────────────────────────────────


@dataclass
class APNProbateResult:
    """End-to-end outcome for one APN-scraped probate notice."""

    notice: NoticeData
    county: str  # "Jefferson" | "Madison"

    property_found: bool = False
    matched_county: str = ""
    tier: Optional[int] = None
    in_target_zip: bool = False

    status: str = "unknown"  # enriched | dropped_off_target | dropped_no_property | error
    notes: str = ""

    @property
    def situs_address(self) -> str:
        return self.notice.address

    @property
    def situs_city(self) -> str:
        return self.notice.city

    @property
    def situs_zip(self) -> str:
        return self.notice.zip


# ── Pipeline ─────────────────────────────────────────────────────────


def _filter_probate_searches(
    searches, counties: tuple[str, ...] = ("Jefferson", "Madison", "Marshall"),
) -> list:
    """Keep only the SAVED_SEARCHES entries for probate in the chosen counties."""
    keep = []
    counties_lc = {c.lower() for c in counties}
    for s in searches:
        if s.notice_type != "probate":
            continue
        if s.county.lower() not in counties_lc:
            continue
        keep.append(s)
    return keep


async def run_pipeline(
    counties: tuple[str, ...] = ("Jefferson", "Madison", "Marshall"),
    days_back: int = 7,
    tier_filter: tuple[int, ...] = (1, 2),
    max_notices: int = 100,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[APNProbateResult], FunnelCounter, ServiceRateTracker]:
    """End-to-end APN post-probate pipeline.

    Args:
        counties: Which counties to scrape ("Jefferson", "Madison", or both).
        days_back: Lookback window for the APN scrape.
        tier_filter: ZIP tiers to keep. (1, 2) = Tier 1 ∪ Tier 2. () = all.
        max_notices: Hard cap on notices processed per run.
        funnel: Optional caller-supplied counter. When omitted, a fresh
            FunnelCounter("apn_probate", gates=...) is constructed
            internally per CONTEXT.md D-01 gate sequence.
        rate_tracker: Optional caller-supplied tracker. When omitted, a
            fresh ServiceRateTracker is constructed. Threaded into
            ``scrape_all`` (NOTE: deferred to plan 02-04 — scraper.py
            doesn't yet accept ``rate_tracker``, so captcha rate may
            read 0/0 in this plan's runs).

    Returns:
        ``(results, funnel, rate_tracker)`` — the per-notice results
        plus the populated funnel + tracker, so notify_slack can append
        the funnel + rates blocks to the run summary.
    """
    # Per CONTEXT.md D-01: apn_probate gate sequence is pre-seeded so the
    # Slack block always renders all 6 gates (zero-count gates are a real
    # signal — "all dropped at decedent-name-searched" must be visible).
    if funnel is None:
        funnel = FunnelCounter("apn_probate", gates=[
            "scraped", "seen_ids_deduped", "decedent_name_searched",
            "tier_gated", "tracerfy_matched", "datasift_uploaded",
        ])
    if rate_tracker is None:
        rate_tracker = ServiceRateTracker()

    searches = _filter_probate_searches(cfg.SAVED_SEARCHES, counties=counties)
    if not searches:
        logger.warning("No probate searches matching counties=%s", counties)
        return ([], funnel, rate_tracker)

    logger.info("APN probate scrape: %d search(es) for counties=%s",
                len(searches), counties)
    since_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    # NOTE: scraper.scrape_all does not yet accept rate_tracker (per the
    # 02-03 plan instructions — that wiring is deferred to plan 02-04,
    # which owns full_pipeline.py and may need scraper.py changes).
    # 2Captcha solve rate will therefore read 0/0 in pure-apn_probate
    # runs until 02-04 lands. Once it does, add `rate_tracker=rate_tracker`
    # to this call.
    notices = await scrape_all(
        mode="custom",
        searches=searches,
        since_date_override=since_date,
        max_notices=max_notices,
    )
    logger.info("Scraped %d probate notice(s) from APN", len(notices))

    # Funnel: scraped + seen_ids_deduped.
    # scrape_all handles seen_ids.json dedup internally (only NEW notices
    # are returned), so the count returned is already post-dedup. Same
    # value for both gates — the plan explicitly accepts this pattern
    # (per the Task 1 action note: "If scrape_all already returns
    # post-dedup count, use the same value for both...").
    funnel.set("scraped", len(notices))
    funnel.set("seen_ids_deduped", len(notices))

    results: list[APNProbateResult] = []
    # Same-person dedupe (P0 #2): APN occasionally publishes the same
    # decedent twice in the same window (republished by a second newspaper,
    # or duplicate scrape with different notice IDs). Observed in live
    # testing: Rex M. Kelley + Jeffrey Lynn Williams each kept TWICE in
    # the 13-day window. Key = (first-3-chars + last-name + granted_date).
    seen_decedents: set[str] = set()
    # Same-property dedupe (P0 #3): when two probate notices for different
    # decedents resolve to the same situs address (co-deceased spouses,
    # estate-on-estate filings, etc.) we already have the lead — duplicate
    # entries waste downstream enrichment + Tracerfy spend.
    seen_addresses: set[tuple[str, str]] = set()
    for n in notices:
        result = APNProbateResult(notice=n, county=n.county or "")

        if n.notice_type != "probate":
            result.status = "error"
            result.notes = f"Unexpected notice_type {n.notice_type!r}"
            results.append(result)
            continue

        # Stage 1.5: Same-person dedupe (P0 #2) — applied BEFORE the
        # expensive property locator. APN probate notices include
        # decedent_name + granted_date which together produce a robust
        # dedup key. Skip duplicates with explicit status so they show up
        # in the summary stats.
        decedent_key = _normalize_decedent_key(
            n.decedent_name or "", n.granted_date or "",
        )
        if decedent_key and decedent_key in seen_decedents:
            result.status = "dropped_duplicate_decedent"
            result.notes = f"duplicate decedent (key={decedent_key})"
            logger.info("  SKIP %s (%s, dup decedent): %s",
                        n.case_number or "?", n.county, n.decedent_name)
            results.append(result)
            continue
        if decedent_key:
            seen_decedents.add(decedent_key)

        # Stage 2: probate_property_locator — searches the county's API by
        # decedent name (Tier 1) and PR name (Tier 2 fallback) and writes
        # address/city/zip onto the notice.
        try:
            matched = enrich_notice_with_property(n)
        except Exception as e:
            logger.warning("Property locator failed for %s: %s", n.decedent_name, e)
            result.status = "error"
            result.notes = f"locator error: {e}"
            results.append(result)
            continue

        if not matched or not n.address:
            result.status = "dropped_no_property"
            result.notes = f"No parcel found for decedent={n.decedent_name!r} pr={n.owner_name!r}"
            logger.info("  DROP %s (%s, no property): %s",
                        n.case_number or "?", n.county, n.decedent_name)
            results.append(result)
            continue

        result.property_found = True
        result.matched_county = n.county

        # Stage 3: AssuranceWeb counties — recover ZIP via Smarty if locator
        # didn't fill it (Madison/Marshall name-search responses lack city/zip;
        # only situs_address). Jefferson E-Ring populates ZIP directly so it
        # never enters this branch.
        county_lc = n.county.lower()
        if not n.zip and n.address and county_lc in {"madison", "marshall"}:
            # 3-tuple variant with city-tier centroid fallback (2026-07-08).
            # When USPS-CASS doesn't recognize the specific house number
            # but Smarty confirms the street is in a known Madison/Marshall
            # city, use the city's Tier-1 centroid ZIP + flag it so
            # downstream filter presets can exclude if precision matters.
            if county_lc == "marshall":
                city, zip_code, zip_estimated = smarty_zip_or_city_estimate_for_marshall(n.address)
            else:
                city, zip_code, zip_estimated = smarty_zip_or_city_estimate_for_madison(n.address)
            if zip_code:
                n.zip = zip_code
                if not n.city and city:
                    n.city = city
                if zip_estimated:
                    existing = n.missing_data_flags or ""
                    n.missing_data_flags = (
                        f"{existing}|zip_estimated_from_city" if existing
                        else "zip_estimated_from_city"
                    )
                logger.debug("  Smarty filled %s ZIP: %s → %s, %s (estimated=%s)",
                             n.county, n.address, city, zip_code, zip_estimated)

        # Stage 3.7: Same-property dedupe (P0 #3) — skip duplicate addresses
        # before the tier gate. Multiple probate notices for co-deceased
        # spouses on the same parcel get one entry in the marketing CSV.
        addr_key = (
            (n.address or "").strip().upper(),
            (n.zip or "").strip()[:5],
        )
        if addr_key[0] and addr_key in seen_addresses:
            result.status = "dropped_duplicate_property"
            result.notes = (
                f"duplicate property — already processed {addr_key[0]} "
                f"@ {addr_key[1] or 'no-zip'}"
            )
            logger.info("  SKIP %s (%s, dup property %s): %s",
                        n.case_number or "?", n.county, addr_key[0],
                        n.decedent_name)
            results.append(result)
            continue
        if addr_key[0]:
            seen_addresses.add(addr_key)

        # Stage 4: ZIP tier gate
        tier, _zone_county = zip_tier_county(n.zip)
        result.tier = tier

        if tier_filter and (tier is None or tier not in tier_filter):
            result.status = "dropped_off_target"
            result.notes = f"ZIP {n.zip or '(empty)'} not in tier filter {tier_filter}"
            logger.info("  DROP %s (%s, tier=%s, zip=%s): %s @ %s",
                        n.case_number or "?", n.county, tier, n.zip,
                        n.decedent_name, n.address)
            results.append(result)
            continue

        result.in_target_zip = True
        result.status = "enriched"
        logger.info("  KEEP %s T%s zip=%s county=%s: %s @ %s",
                    n.case_number or "?", tier, n.zip, n.county,
                    n.decedent_name, n.address)
        results.append(result)

    # Funnel: post-loop gate counts (D-01 per-pipeline sequence).
    # - decedent_name_searched: notices whose property locator returned a
    #   parcel (parcel_id populated). Drops where the locator returned
    #   nothing roll up as the implicit drop between this gate and the
    #   tier_gated one above.
    # - tier_gated: notices that survived the tier_filter (status==enriched).
    # tracerfy_matched and datasift_uploaded are populated downstream by
    # prepare_notices() and the CSV writer respectively — they default to
    # 0 (pre-seeded) and update if/when those stages run.
    funnel.set(
        "decedent_name_searched",
        sum(1 for r in results if r.notice.parcel_id),
    )
    funnel.set(
        "tier_gated",
        sum(1 for r in results if r.status == "enriched"),
    )

    return (results, funnel, rate_tracker)


# ── DataSift CSV + skip-trace ────────────────────────────────────────


def prepare_notices(
    results: list[APNProbateResult],
    enriched_only: bool = True,
    skip_trace: bool = False,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[NoticeData], dict | None]:
    """Pull NoticeData from enriched results and optionally run skip-trace.

    Unlike pre_probate_pipeline.prepare_notices, our notices are ALREADY
    in NoticeData shape (the APN scraper produces them directly), so we
    don't need a CaseResult→NoticeData converter. Just filter, optionally
    skip-trace, optionally promote heir contacts.

    When ``funnel`` is supplied, sets ``tracerfy_matched`` from skip-trace
    stats. When ``rate_tracker`` is supplied, it's threaded into
    ``batch_skip_trace`` (Wave 2 contract) so Tracerfy success rates feed
    into the per-run + 7-day rolling rates.
    """
    eligible = [r for r in results if not enriched_only or r.status == "enriched"]
    notices = [r.notice for r in eligible]
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
            logger.warning("Skip-trace failed: %s", e)
            stats = {"error": str(e)}

    return notices, stats


# ── Slack ────────────────────────────────────────────────────────────


def build_slack_message(
    results: list[APNProbateResult],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
    days_back: int = 7,
) -> str:
    """Build a concise APN-post-probate Slack message.

    Same shape as the Benchmark Slack message but APN-specific: no case
    docket detail (APN gives us notice publication only), county tag per lead.
    """
    total = len(results)
    by_status = Counter(r.status for r in results)
    enriched = [r for r in results if r.status == "enriched"]
    by_tier = Counter(r.tier for r in enriched)
    by_county = Counter(r.county for r in enriched)

    lines: list[str] = []
    today = date.today().strftime("%Y-%m-%d")
    lines.append(f"*Alabama Post-Probate (APN newspaper-pub) — {today}* (last {days_back}d)")
    lines.append(
        f"  scraped: {total}  ·  in-tier: {len(enriched)} "
        f"(T1: {by_tier.get(1, 0)}  T2: {by_tier.get(2, 0)})  ·  "
        f"Jefferson: {by_county.get('Jefferson', 0)}  ·  "
        f"Madison: {by_county.get('Madison', 0)}  ·  "
        f"off-tier: {by_status.get('dropped_off_target', 0)}  ·  "
        f"no-property: {by_status.get('dropped_no_property', 0)}  ·  "
        f"errors: {by_status.get('error', 0)}"
    )

    if not enriched:
        lines.append("")
        lines.append("_No new in-tier APN probate leads this run._")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"*New leads — {len(enriched)}*")

    for r in enriched:
        n = r.notice
        addr = n.address or "(address unknown)"
        val = ""
        if n.assessed_value:
            try:
                val = f"${float(n.assessed_value):,.0f}"
            except (ValueError, TypeError):
                val = f"${n.assessed_value}"
        else:
            val = "$?"
        tier_label = f"T{r.tier}·{r.county}" if r.tier else f"T?·{r.county}"
        flags: list[str] = []
        if n.is_homestead == "Y":
            flags.append("homestead")
        flag_str = "  ·  " + " · ".join(flags) if flags else ""

        lines.append("")
        lines.append(
            f"• *{addr}*, {n.city} {n.zip}  ·  {tier_label}  ·  {val}{flag_str}"
        )
        case_label = f"  case `{n.case_number}`" if n.case_number else ""
        lines.append(f"   {case_label}  decedent: {n.decedent_name or '?'}")
        if n.owner_name and n.owner_name != n.decedent_name:
            lines.append(f"    PR: {n.owner_name}")
        if n.granted_date:
            lines.append(f"    granted: {n.granted_date}  creditor deadline: {n.creditor_deadline or '?'}")
        if n.judge_name:
            lines.append(f"    judge: {n.judge_name}")
        if n.source_url:
            lines.append(f"    source: <{n.source_url}|APN notice>")

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
    results: list[APNProbateResult],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
    days_back: int = 7,
    webhook_url: str | None = None,
    *,
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> bool:
    """Post the APN post-probate run summary to Slack/Discord.

    When ``funnel`` and ``rate_tracker`` are BOTH provided (Phase 2
    block-aware path), this function:

      1. Loads the 7-day rolling baseline FIRST (so today's post reflects
         the rolling rate computed from yesterday-and-prior days — NOT
         today's totals).
      2. Builds a 3-block payload: existing summary text + funnel block +
         service-rates block.
      3. POSTs via ``_send_blocks_webhook`` (one HTTP call, one message —
         D-02 honored: "one message, more content").
      4. AFTER a successful send, calls ``save_rolling_rates`` so today's
         totals become tomorrow's baseline. A failed send leaves the
         rolling baseline untouched so the bad run doesn't pollute the
         window.

    Legacy callers (no funnel + no rate_tracker) get the byte-identical
    plain-text-only path through ``_send_webhook`` for backward compat.
    """
    text = build_slack_message(
        results, csv_path=csv_path, skip_trace_stats=skip_trace_stats, days_back=days_back,
    )

    # Legacy path: no funnel + no tracker → plain text via _send_webhook,
    # byte-identical to the pre-Phase-2 behaviour.
    if funnel is None and rate_tracker is None:
        import slack_notifier
        sent = slack_notifier._send_webhook(text, webhook_url=webhook_url)
        if sent:
            logger.info("Slack notification sent (%d enriched, legacy text-only)",
                        sum(1 for r in results if r.status == "enriched"))
        else:
            logger.warning("Slack notification failed (no webhook or send error)")
        return sent

    # Phase 2 block-aware path. Per CONTEXT.md D-03 + plan-checker W6:
    # rolling-rates ordering is mandatory — load FIRST (yesterday's
    # baseline shows on today's post), save AFTER successful send (today's
    # totals advance the window for tomorrow's baseline).
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


def _print_summary(results: list[APNProbateResult]) -> None:
    total = len(results)
    by_status = Counter(r.status for r in results)
    enriched = [r for r in results if r.status == "enriched"]
    by_tier = Counter(r.tier for r in enriched)
    by_county = Counter(r.county for r in enriched)

    print(f"\n{'═' * 64}")
    print(f"  APN post-probate pipeline — {total} notice(s) scraped")
    print(f"{'═' * 64}")
    print(f"  enriched (in target ZIP):     {by_status['enriched']}")
    print(f"    Tier 1:                     {by_tier.get(1, 0)}")
    print(f"    Tier 2:                     {by_tier.get(2, 0)}")
    print(f"    Jefferson:                  {by_county.get('Jefferson', 0)}")
    print(f"    Madison:                    {by_county.get('Madison', 0)}")
    print(f"  dropped (off-target ZIP):     {by_status.get('dropped_off_target', 0)}")
    print(f"  dropped (no property):        {by_status.get('dropped_no_property', 0)}")
    print(f"  errors:                       {by_status.get('error', 0)}")
    print()

    if enriched:
        print(f"  ━━━ Enriched leads ━━━")
        for r in enriched:
            n = r.notice
            val = ""
            try:
                val = f" ${float(n.assessed_value):,.0f}" if n.assessed_value else ""
            except (ValueError, TypeError):
                pass
            print(f"  • {n.address[:40]}, {n.city} {n.zip}  T{r.tier}·{r.county}{val}")
            print(f"      decedent: {n.decedent_name}  PR: {n.owner_name}")
            if n.granted_date:
                print(f"      granted: {n.granted_date}  case: {n.case_number}")
            if n.source_url:
                print(f"      apn: {n.source_url}")
    print()


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Run the Alabama APN-driven post-probate pipeline (Jefferson + Madison + Marshall).",
    )
    p.add_argument("--counties", type=str, default="Jefferson,Madison,Marshall",
                   help="Comma-separated counties (default: Jefferson,Madison,Marshall).")
    p.add_argument("--days-back", type=int, default=7,
                   help="APN lookback window in days (default: 7)")
    p.add_argument("--max-notices", type=int, default=100,
                   help="Hard cap on notices scraped per run (default: 100)")
    p.add_argument("--tiers", type=str, default="1,2",
                   help="Comma-separated tier filter: '1', '2', '1,2', or 'none' (default: 1,2)")
    p.add_argument("--datasift-csv", action="store_true",
                   help="Write enriched results to a DataSift-formatted CSV.")
    p.add_argument("--skip-trace", action="store_true",
                   help="With --datasift-csv: also run Tracerfy skip-trace.")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post run summary to Slack/Discord.")
    p.add_argument("--json", action="store_true",
                   help="Output JSON instead of summary.")
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
    counties = tuple(c.strip() for c in args.counties.split(",") if c.strip())

    results, funnel, rate_tracker = asyncio.run(run_pipeline(
        counties=counties,
        days_back=args.days_back,
        tier_filter=tier_filter,
        max_notices=args.max_notices,
    ))

    if args.json:
        # Crude JSON dump — NoticeData has many fields, so just dump the diagnostic shape
        payload = [{
            "case_number": r.notice.case_number,
            "county": r.county,
            "decedent_name": r.notice.decedent_name,
            "address": r.notice.address,
            "city": r.notice.city,
            "zip": r.notice.zip,
            "tier": r.tier,
            "status": r.status,
            "notes": r.notes,
        } for r in results]
        print(json.dumps(payload, indent=2))
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
            # Trestle phone scoring — same gap fix as pre_probate (2026-06-13).
            # apn_probate runs Tracerfy too but had no scoring step, so
            # Phone Tags N columns were empty on uploaded records.
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

    # D-04 — terminal mirrors Slack: always log the funnel at end-of-run
    # so the operator sees drop counts even when --notify-slack isn't set.
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
            days_back=args.days_back,
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
