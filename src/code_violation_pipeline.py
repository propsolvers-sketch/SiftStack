"""Unified code-violation orchestrator for Madison + Jefferson Counties, AL.

Single entry point for the daily code-enforcement feed. Runs both county
adapters in one pass, converts to NoticeData, and optionally writes the
standard Sift CSV and/or the DataSift-formatted CSV.

  Madison    → Huntsville Unsafe Buildings (PDF list, ~222 cases / month)
                Phase 1 — formal "demolish" tier only.
                See: huntsville_unsafe_buildings_api.py
  Jefferson  → Birmingham Accela Citizen Access (5 enforcement record types)
                Phase 4 — full distress funnel: condemnation + housing
                + inoperable vehicles + environmental + zoning.
                Adds ~300-500 records / month with `early_distress` tag.
                See: birmingham_code_enforcement_api.py

Owner enrichment (opt-in, ``--enrich-owner``):
  Both adapters now expose tax-roll address-search:
    Madison    → search_by_situs_address (AssuranceWeb)         ~80% hit rate
    Jefferson  → search_by_situs_address (E-Ring searchtype=4)  ~80% hit rate
  Cheap (~0.3s/rec, no Playwright cost) and runs whether or not Birmingham's
  Accela detail-page enrichment is requested.

Detail-page enrichment (opt-in, ``--enrich-details``):
  Birmingham only — clicks each case to extract fee_total, fee_balance,
  mailing address, and Accela's deceased-flag annotations. Slow (~3s/rec)
  but surfaces fines that can't be derived from the tax roll.

Phase 2 (OPS-03 / OBS-01) — funnel transparency
================================================
``fetch_code_violations`` now records a ``FunnelCounter("code_violation")``
with the canonical 3-gate D-01 sequence (bulk_fetched → owner_enriched →
tier_gated) and threads a single ``ServiceRateTracker`` through the
adapter ``to_notice_data(..., enrich_owner=True)`` paths (which hit the
Madison + Jefferson tax-roll address-search APIs). When ``--notify-slack``
is set, the CLI posts a single Slack Block Kit message containing the
per-run funnel + service-rates blocks via
``slack_notifier._send_blocks_webhook`` (D-02 — one message per run).
Rolling rates are loaded BEFORE blocks build (so today's post reflects
yesterday's baseline) and saved AFTER successful send (so today's totals
advance the window for tomorrow). See
``.planning/phases/02-funnel-transparency/02-CONTEXT.md``.

CLI
====
    # Both counties, default windows
    python src/code_violation_pipeline.py

    # Birmingham early-distress only, 60-day window, 10 pages per category
    python src/code_violation_pipeline.py --counties Jefferson \\
        --days 60 --max-pages 10

    # Phase 1 high-conversion subset (Huntsville cases ≥ 2 years old) +
    # Birmingham condemnation only — pure tear-down lead pull
    python src/code_violation_pipeline.py --min-age-years 2 \\
        --categories condemnation

    # Full feed with owner enrichment + DataSift CSV
    python src/code_violation_pipeline.py --enrich-owner \\
        --output-datasift-csv output/code_violations_datasift.csv

    # Daily-ops with Slack funnel post
    python src/code_violation_pipeline.py --enrich-owner \\
        --output-datasift-csv output/code_violations_datasift.csv \\
        --notify-slack
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from notice_parser import NoticeData
from observability import (
    FunnelCounter,
    ServiceRateTracker,
    load_rolling_rates,
    rolling_rates_summary,
    save_rolling_rates,
)
from slack_notifier import (
    _send_blocks_webhook,
    build_funnel_block,
    build_service_rates_block,
)

logger = logging.getLogger(__name__)


# Default Birmingham categories — every Accela enforcement record type
# except "Environmental Batch Record" (low individual-record value).
_DEFAULT_BIRM_CATEGORIES = (
    "housing", "vehicles", "environmental", "zoning", "condemnation",
)


# ── Phase 2 canonical gate sequence (CONTEXT.md D-01) ────────────────

CODE_VIOLATION_GATES: tuple[str, ...] = (
    "bulk_fetched",
    "owner_enriched",
    "tier_gated",
)
"""Canonical 3-gate D-01 sequence for the code-violation pipeline. Pinned
as a module constant so any rollup (e.g. Phase 3 unified scheduler) can
reuse the ordered list without re-deriving it."""


# ── Per-county fetch wrappers ────────────────────────────────────────


def _fetch_madison(
    *,
    min_age_years: int,
    enrich_owner: bool,
) -> list[NoticeData]:
    """Pull the Huntsville Unsafe Buildings PDF list and convert to NoticeData."""
    from huntsville_unsafe_buildings_api import (
        fetch_unsafe_buildings, to_notice_data,
    )
    records = fetch_unsafe_buildings(min_age_years=min_age_years)
    return [to_notice_data(r, enrich_owner=enrich_owner) for r in records]


def _fetch_jefferson(
    *,
    categories: tuple[str, ...],
    days_back: int,
    max_pages: int,
    enrich_details: bool,
    enrich_owner: bool,
    headless: bool,
) -> list[NoticeData]:
    """Run the Birmingham Accela scraper and convert to NoticeData."""
    from birmingham_code_enforcement_api import (
        fetch_enforcement_cases, to_notice_data,
    )
    records = fetch_enforcement_cases(
        categories=list(categories),
        days_back=days_back,
        max_pages=max_pages,
        enrich_details=enrich_details,
        headless=headless,
    )
    return [to_notice_data(r, enrich_owner=enrich_owner) for r in records]


def _fetch_hoover(
    *,
    days_back: int,
    enrich_owner: bool,
    target_zips_only: bool,
) -> list[NoticeData]:
    """Run the Hoover SeeClickFix code-enforcement adapter and convert to NoticeData.

    Hoover is in Jefferson County but has its own platform (SeeClickFix 311
    citizen-complaint feed) — separate from Birmingham Accela. Citizen-reported
    violations land here within hours; we filter to the dedicated
    "CODE ENFORCEMENT" request_type and (optionally) our Tier 1/Tier 2 ZIPs.
    """
    from hoover_code_enforcement_api import fetch_code_violations, to_notice_data
    target_zips = None
    if target_zips_only:
        from target_zips import ALL_TARGET
        target_zips = set(ALL_TARGET)
    records = fetch_code_violations(
        days_back=days_back,
        target_zips=target_zips,
    )
    return [to_notice_data(r, enrich_owner=enrich_owner) for r in records]


# ── Public API ───────────────────────────────────────────────────────


def fetch_code_violations(
    *,
    counties: tuple[str, ...] = ("Madison", "Jefferson", "Marshall"),
    # Birmingham (Jefferson) knobs
    categories: tuple[str, ...] = _DEFAULT_BIRM_CATEGORIES,
    days_back: int = 30,
    max_pages: int = 5,
    enrich_details: bool = False,
    headless: bool = True,
    # Huntsville (Madison) knobs
    min_age_years: int = 0,
    # Hoover (Jefferson, separate platform) knobs
    include_hoover: bool = True,
    hoover_target_zips_only: bool = True,
    # Shared
    enrich_owner: bool = False,
    tiers: tuple[int, ...] | None = (1, 2),
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[NoticeData], FunnelCounter, ServiceRateTracker]:
    """Pull the full code-violation feed for the requested AL counties.

    Args:
        counties: Counties to query. Case-insensitive on input. Default both.
            ``"Madison"`` → Huntsville Unsafe Buildings;
            ``"Jefferson"`` → Birmingham Accela + Hoover SeeClickFix
            (Hoover is in Jefferson County but uses a separate platform).
        categories: Birmingham Accela record-type CLI keys to query
            (housing, vehicles, environmental, zoning, condemnation).
            Ignored when Jefferson is not selected.
        days_back: Window for Birmingham AND Hoover (both honor it).
        max_pages: Birmingham per-category pagination cap (default 5).
        enrich_details: Birmingham only — click each case detail page for
            fees + mailing address. Slow (~3s/case).
        headless: Birmingham only — set False for visible Playwright debug.
        min_age_years: Huntsville only — drop cases newer than N whole years
            (Phase 1 high-conversion subset uses 2).
        include_hoover: When Jefferson is selected, also pull Hoover
            SeeClickFix issues (default True). Hoover citizen-complaints are
            an early-distress signal class distinct from Birmingham Accela
            formal cases.
        hoover_target_zips_only: Filter Hoover output to our Tier 1+2 ZIPs
            (default True — Hoover spans 35022 T1, 35226 T1, 35216 T2,
            35244 T2 plus off-target spillover into Vestavia / Pelham).
        enrich_owner: All cities — opt-in tax-roll address-search to fill
            owner of record (~0.3s/case, ~80% hit rate). For Birmingham,
            Jefferson tax-roll fires first; Accela detail-page only runs
            for cases the tax-roll missed. For Hoover, Jefferson tax-roll
            via E-Ring (same as Birmingham). For Huntsville, Madison
            address-search.
        funnel: Optional pre-constructed FunnelCounter. When omitted, a
            fresh FunnelCounter("code_violation", gates=CODE_VIOLATION_GATES)
            is built — pre-seeded with all 3 D-01 gates so the Slack block
            always renders the full sequence.
        rate_tracker: Optional pre-constructed ServiceRateTracker. When
            omitted, a fresh one is built. Wave 2 service-call instrumentation
            inside the adapter modules (Smarty, LLM) records into the
            supplied tracker automatically when threaded through the
            adapter entry points.

    Returns:
        Tuple of (notices, funnel, rate_tracker). The funnel + tracker are
        returned for ``notify_slack`` / terminal logging in the CLI path.
        Combined NoticeData list across all requested cities, in the
        order Madison → Birmingham → Hoover.
    """
    if funnel is None:
        funnel = FunnelCounter("code_violation", gates=list(CODE_VIOLATION_GATES))
    if rate_tracker is None:
        rate_tracker = ServiceRateTracker()

    selected = {c.strip().title() for c in counties if c}
    notices: list[NoticeData] = []

    if "Madison" in selected:
        logger.info("Fetching Huntsville Unsafe Buildings list (Madison)…")
        notices.extend(_fetch_madison(
            min_age_years=min_age_years,
            enrich_owner=enrich_owner,
        ))
    if "Jefferson" in selected:
        logger.info(
            "Fetching Birmingham Accela enforcement cases (Jefferson) — "
            "categories=%s, days_back=%d", list(categories), days_back,
        )
        notices.extend(_fetch_jefferson(
            categories=categories,
            days_back=days_back,
            max_pages=max_pages,
            enrich_details=enrich_details,
            enrich_owner=enrich_owner,
            headless=headless,
        ))
        if include_hoover:
            logger.info(
                "Fetching Hoover SeeClickFix code-enforcement issues — "
                "days_back=%d, target_zips_only=%s",
                days_back, hoover_target_zips_only,
            )
            notices.extend(_fetch_hoover(
                days_back=days_back,
                enrich_owner=enrich_owner,
                target_zips_only=hoover_target_zips_only,
            ))

    if "Marshall" in selected:
        # Marshall has NO dedicated online code-enforcement source — researched
        # 2026-05-12: no Accela / SeeClickFix / municipal portal exposes
        # citizen complaints or formal cases. Marshall code-violation coverage
        # is APN-floor-only (the 3 SAVED_SEARCHES entries in config.py pick up
        # statewide DEMOLITION/CONDEMNATION/NUISANCE-ABATEMENT publications,
        # which include Marshall when they're filed). That path runs through
        # the daily APN scraper, not through this pipeline — so the right
        # behavior here is a no-op log, not an error.
        logger.info(
            "Marshall: no dedicated code-enforcement adapter — APN-floor "
            "coverage runs via the daily APN scraper (SAVED_SEARCHES) "
            "instead. Skipping in this orchestrator.",
        )

    unknown = selected - {"Madison", "Jefferson", "Marshall"}
    if unknown:
        logger.warning("Unknown counties skipped: %s", sorted(unknown))

    # ── Gate 1: bulk_fetched ──────────────────────────────────────────
    funnel.set("bulk_fetched", len(notices))

    # ── Gate 2: owner_enriched ────────────────────────────────────────
    # When --enrich-owner is on, each adapter's to_notice_data populates
    # owner_name via tax-roll address-search. Count notices with a
    # populated owner_name as the survivor set. When --enrich-owner is
    # off, the gate is a pass-through (count = bulk_fetched) so the
    # funnel always renders the full 3-gate sequence per D-01 invariant.
    if enrich_owner:
        owner_count = sum(1 for n in notices if n.owner_name)
    else:
        owner_count = len(notices)
    funnel.set("owner_enriched", owner_count)
    logger.info(
        "owner_enriched gate: %d notices have an owner_name "
        "(enrich_owner=%s)",
        owner_count, enrich_owner,
    )

    # ── Gate 3: tier_gated ────────────────────────────────────────────
    # Tier-ZIP filter — drops Birmingham + Huntsville records outside our
    # investor-target ZIPs. Hoover is already tier-filtered upstream when
    # `hoover_target_zips_only=True`, but running the filter here is a
    # cheap no-op safety net for that path too.
    if tiers:
        from target_zips import zip_tier
        tier_set = set(tiers)
        before = len(notices)
        kept = [n for n in notices if zip_tier(n.zip) in tier_set]
        dropped = before - len(kept)
        logger.info(
            "Tier filter (tiers=%s): %d → %d (dropped %d, incl. records w/ no ZIP)",
            sorted(tier_set), before, len(kept), dropped,
        )
        notices = kept
    funnel.set("tier_gated", len(notices))

    # ── Within-run dedup ──────────────────────────────────────────────
    # Same code-violation case can appear across multiple feeds:
    #   * Huntsville Unsafe Buildings PDF re-publishes the same property
    #     month over month while the case is still open
    #   * Birmingham Accela returns historical entries on each pull within
    #     the --days-back window
    #   * Hoover SeeClickFix can list the same nuisance via multiple
    #     citizen reports against the same address
    # Operator reported duplicate uploads to DataSift (2026-06-10). Dedup
    # on the strongest identity available — (normalized address, case#) —
    # so the same property at the same case doesn't ride through twice.
    # Falls back to address-only when case# is missing (some adapters
    # don't populate it consistently).
    seen: set[tuple[str, str]] = set()
    deduped: list[NoticeData] = []
    drops = 0
    for n in notices:
        addr_key = (n.address or "").strip().lower()
        case_key = (n.case_number or "").strip().lower()
        if not addr_key:
            # No address — pass through (can't dedup safely)
            deduped.append(n)
            continue
        key = (addr_key, case_key)
        if key in seen:
            drops += 1
            continue
        seen.add(key)
        deduped.append(n)
    if drops:
        logger.info(
            "Code-violation dedup: %d → %d (dropped %d duplicates)",
            len(notices), len(deduped), drops,
        )
        notices = deduped

    return notices, funnel, rate_tracker


# ── Phase 2: Slack notification ──────────────────────────────────────


def _build_summary_text(
    notices: list[NoticeData],
    funnel: FunnelCounter,
) -> str:
    """Build the markdown header for the code-violation Slack post.

    Short summary: per-county counts + per-subtype breakdown. Funnel +
    service rates render in the following blocks (D-02 — one message,
    three blocks).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    by_county: dict[str, int] = {}
    by_sub: dict[str, int] = {}
    for n in notices:
        by_county[n.county or "(unknown)"] = by_county.get(n.county or "(unknown)", 0) + 1
        key = n.notice_subtype or "(unspecified)"
        by_sub[key] = by_sub.get(key, 0) + 1

    parts = [f"*Code-Violation Run — {today}*"]
    if notices:
        per_county = ", ".join(
            f"{county}: {count}"
            for county, count in sorted(by_county.items())
        )
        parts.append(f"{len(notices)} records ({per_county})")
        subtype_summary = ", ".join(
            f"{sub}={count}"
            for sub, count in sorted(by_sub.items(), key=lambda kv: -kv[1])
        )
        parts.append(f"By subtype: {subtype_summary}")
    else:
        parts.append("0 records in-tier this run.")
    return "\n".join(parts)


def notify_slack(
    notices: list[NoticeData],
    funnel: FunnelCounter,
    rate_tracker: ServiceRateTracker,
    *,
    webhook_url: str | None = None,
) -> bool:
    """Post the code-violation run summary to Slack/Discord as a single message.

    Per CONTEXT.md D-02: exactly one HTTP call per run. Block-aware payload
    = summary header + funnel block + service-rates block.

    Rolling-rates ordering (D-03 / W6):
      1. load_rolling_rates BEFORE blocks build (today's post shows
         yesterday-and-prior baseline, not today's totals).
      2. _send_blocks_webhook runs next.
      3. save_rolling_rates(rate_tracker.totals()) runs LAST, ONLY if the
         send succeeded — failed sends never pollute the 7-day window.
    """
    text = _build_summary_text(notices, funnel)

    rolling = rolling_rates_summary(load_rolling_rates())
    per_run = rate_tracker.per_run_rates()

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        build_funnel_block(funnel.pipeline_name, funnel.as_ordered_dict()),
        build_service_rates_block(per_run, rolling),
    ]

    sent = _send_blocks_webhook(text, blocks, webhook_url=webhook_url)
    if sent:
        save_rolling_rates(rate_tracker.totals())
        logger.info(
            "Slack notification sent (%d records, blocks payload + rolling saved)",
            len(notices),
        )
    else:
        logger.warning("Slack notification failed (no webhook or send error)")
    return sent


# ── CLI ──────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="code_violation_pipeline",
        description="Unified Madison (Huntsville) + Jefferson (Birmingham) "
                    "code-enforcement pull.",
    )
    p.add_argument(
        "--counties", default="Madison,Jefferson,Marshall",
        help="Comma-separated counties (default: Madison,Jefferson,Marshall). "
             "Marshall accepted but no-ops (APN-floor coverage only — runs via "
             "the daily APN scraper, not this orchestrator).",
    )
    # Birmingham (Jefferson) knobs
    p.add_argument(
        "--categories", default=",".join(_DEFAULT_BIRM_CATEGORIES),
        help="Birmingham Accela categories (housing,vehicles,environmental,"
             "zoning,condemnation). Default: all 5.",
    )
    p.add_argument(
        "--days", type=int, default=30, dest="days_back",
        help="Birmingham search window in days (default 30).",
    )
    p.add_argument(
        "--max-pages", type=int, default=5,
        help="Birmingham per-category pagination cap (default 5).",
    )
    p.add_argument(
        "--enrich-details", action="store_true",
        help="Birmingham only — click each case for fees + mailing address "
             "(~3s/case).",
    )
    p.add_argument(
        "--no-headless", action="store_true",
        help="Birmingham only — show Playwright browser for debugging.",
    )
    # Huntsville (Madison) knobs
    p.add_argument(
        "--min-age-years", type=int, default=0,
        help="Huntsville only — drop cases newer than N whole years "
             "(Phase 1 high-conversion subset uses 2).",
    )
    # Hoover (Jefferson, separate platform) knobs
    p.add_argument(
        "--no-hoover", action="store_true",
        help="When Jefferson is selected, skip the Hoover SeeClickFix pull "
             "(default: Hoover is included).",
    )
    p.add_argument(
        "--hoover-all-zips", action="store_true",
        help="Hoover only — pull ALL Hoover code-enforcement issues "
             "(default: filter to our Tier 1+2 ZIPs only).",
    )
    # Shared
    p.add_argument(
        "--enrich-owner", action="store_true",
        help="Tax-roll owner enrichment for all cities (~80%% hit rate, "
             "~0.3s/case).",
    )
    p.add_argument(
        "--tiers", type=str, default="1,2",
        help="Comma-separated ZIP tiers to keep after pull (default '1,2'). "
             "'all' disables the filter. Drops Birmingham/Huntsville records "
             "outside our investor-target ZIPs.",
    )
    # Output
    p.add_argument(
        "--output-csv", type=Path, default=None,
        help="Write standard Sift-format CSV to this path.",
    )
    p.add_argument(
        "--output-datasift-csv", type=Path, default=None,
        help="Write DataSift-format CSV (80 cols) to this path.",
    )
    p.add_argument(
        "--notify-slack", action="store_true",
        help="Post run summary + funnel + service-rates blocks to Slack "
             "(D-02 — one message per run via SLACK_WEBHOOK_URL).",
    )
    return p


def _summarize(notices: list[NoticeData]) -> None:
    """Pretty-print a per-county / per-subtype breakdown to stdout."""
    by_county: dict[str, list[NoticeData]] = {}
    for n in notices:
        by_county.setdefault(n.county, []).append(n)

    print(f"\n=== Code-violation summary — {len(notices)} total notices ===")
    for county, recs in sorted(by_county.items()):
        by_sub: dict[str, int] = {}
        for r in recs:
            by_sub[r.notice_subtype or "(unspecified)"] = (
                by_sub.get(r.notice_subtype or "(unspecified)", 0) + 1
            )
        owners_filled = sum(1 for r in recs if r.owner_name)
        fee_total = sum(_safe_float(r.tax_delinquent_amount) for r in recs)
        print(f"\n  {county}: {len(recs)} records")
        for subtype, count in sorted(by_sub.items(), key=lambda kv: -kv[1]):
            print(f"    {subtype:<28} {count}")
        print(f"    owner_name filled:           {owners_filled}/{len(recs)}")
        if fee_total > 0:
            print(f"    Fees / fines on file:        ${fee_total:,.2f}")


def _safe_float(s: str) -> float:
    try:
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = _build_argparser().parse_args(argv)

    counties = tuple(c.strip() for c in args.counties.split(",") if c.strip())
    categories = tuple(c.strip() for c in args.categories.split(",") if c.strip())

    tiers_arg = (args.tiers or "").lower()
    if tiers_arg in ("", "all"):
        tiers: tuple[int, ...] | None = None
    else:
        tiers = tuple(int(t) for t in args.tiers.split(",") if t.strip().isdigit())

    notices, funnel, rate_tracker = fetch_code_violations(
        counties=counties,
        categories=categories,
        days_back=args.days_back,
        max_pages=args.max_pages,
        enrich_details=args.enrich_details,
        headless=not args.no_headless,
        min_age_years=args.min_age_years,
        include_hoover=not args.no_hoover,
        hoover_target_zips_only=not args.hoover_all_zips,
        enrich_owner=args.enrich_owner,
        tiers=tiers,
    )

    _summarize(notices)

    # D-04: terminal mirrors Slack regardless of whether --notify-slack is set.
    logger.info(
        "Funnel (%s): %s",
        funnel.pipeline_name, dict(funnel.as_ordered_dict()),
    )

    if args.output_csv:
        from data_formatter import write_csv
        path = write_csv(notices, str(args.output_csv))
        print(f"\nWrote Sift CSV: {path}")
    if args.output_datasift_csv:
        from datasift_formatter import write_datasift_csv
        path = write_datasift_csv(notices, str(args.output_datasift_csv))
        print(f"Wrote DataSift CSV: {path}")

    if args.notify_slack:
        notify_slack(notices, funnel, rate_tracker)

    if not notices:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
