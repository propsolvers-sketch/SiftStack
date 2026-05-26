"""Unified tax-distress orchestrator for Jefferson + Madison Counties, AL.

Runs both county adapters in one pass, converts to NoticeData, stamps auction
dates on tax-sale records (Phase 3), and optionally writes the standard Sift
CSV and/or the DataSift-formatted CSV. Single entry point for the daily
tax-distress feed.

Phase 3 — auction-date stamping
================================
Both counties hold their annual tax-lien auctions in early May (per
AL § 40-10-15 and counties' implementing rules):
  Jefferson: Tuesday of the first full week of May (online @ GovEase / E-Ring)
  Madison:   First week of May (online @ GovEase)

We compute the next "first Tuesday of May" on/after today and stamp it as
the auction_date on every tax_sale-typed record. Records whose notice_type
is still "tax_delinquent" (Madison parcels NOT pre-flagged for the upcoming
auction) are left without an auction date — those don't have a sale scheduled.

Phase 2 (OPS-03 / OBS-01) — funnel transparency
================================================
``fetch_tax_distress`` now records a ``FunnelCounter("tax_distress")`` with
the canonical 5-gate D-01 sequence (bulk_fetched → individual_owner_filtered →
min_balance_filtered → smarty_geocoded → tier_gated) and threads a single
``ServiceRateTracker`` through the AssuranceWeb Smarty geocode path (Madison +
Marshall records lack ZIP in the bulk feed and need Smarty to participate in
the tier gate). When ``--notify-slack`` is set, the CLI posts a single Slack
Block Kit message containing the per-run funnel + service-rates blocks via
``slack_notifier._send_blocks_webhook`` (D-02 — one message per run). Rolling
rates are loaded BEFORE blocks build (so today's post reflects yesterday's
baseline) and saved AFTER successful send (so today's totals advance the
window for tomorrow). See ``.planning/phases/02-funnel-transparency/02-CONTEXT.md``.

CLI
====
    # Both counties, full feed
    python src/tax_distress_pipeline.py

    # Phase 1 canonical filter (individuals only, $5k+ owed)
    python src/tax_distress_pipeline.py --individuals-only --min-balance 5000

    # Single county
    python src/tax_distress_pipeline.py --counties Madison

    # Daily-ops with Slack funnel post
    python src/tax_distress_pipeline.py --individuals-only --min-balance 5000 \\
        --output-datasift-csv output/tax_distress_datasift.csv --notify-slack

    # Write both Sift and DataSift CSVs
    python src/tax_distress_pipeline.py --individuals-only --min-balance 5000 \\
        --output-csv output/tax_distress.csv \\
        --output-datasift-csv output/tax_distress_datasift.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

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


# ── Phase 2 canonical gate sequence (CONTEXT.md D-01) ────────────────

TAX_DISTRESS_GATES: tuple[str, ...] = (
    "bulk_fetched",
    "individual_owner_filtered",
    "min_balance_filtered",
    "smarty_geocoded",
    "tier_gated",
)
"""Canonical 5-gate D-01 sequence for the tax-distress pipeline. Pinned as a
module constant so any rollup (e.g. Phase 3 unified scheduler) can reuse the
ordered list without re-deriving it."""


# ── Phase 3: auction-date helpers ────────────────────────────────────


def next_al_tax_sale_date(today: date | None = None) -> date:
    """Return the next 'first Tuesday of May' on or after ``today``.

    Both Jefferson and Madison schedule their annual tax-lien auctions in
    early May; Jefferson's 2025 auction was on Tuesday May 6, and Madison
    runs the same week. If we're already past this year's first-Tuesday,
    return next year's instead so callers always get a future date.
    """
    if today is None:
        today = date.today()

    def first_tuesday(year: int) -> date:
        may1 = date(year, 5, 1)
        # Monday=0 ... Tuesday=1 ... Sunday=6
        days_until_tuesday = (1 - may1.weekday()) % 7
        return may1 + timedelta(days=days_until_tuesday)

    candidate = first_tuesday(today.year)
    if candidate < today:
        candidate = first_tuesday(today.year + 1)
    return candidate


def apply_auction_dates(notices: Iterable[NoticeData]) -> int:
    """Stamp ``auction_date`` on every tax_sale-typed notice that lacks one.

    Returns the number of notices updated. Madison records that came in as
    ``tax_delinquent`` (i.e. ``is_tax_sale_parcel`` was False on the source
    record) are left alone — those parcels aren't on the upcoming auction's
    roster and shouldn't carry an auction_date.
    """
    auction_date = next_al_tax_sale_date().strftime("%Y-%m-%d")
    updated = 0
    for n in notices:
        if n.notice_type == "tax_sale" and not n.auction_date:
            n.auction_date = auction_date
            updated += 1
    if updated:
        logger.info("Stamped auction_date=%s on %d tax_sale records", auction_date, updated)
    return updated


# ── Per-county fetch wrappers ────────────────────────────────────────


def _fetch_madison_raw():
    """Return raw Madison delinquent records (no filters) for in-pipeline gate counting."""
    from madison_tax_delinquent_api import fetch_delinquent_parcels
    return fetch_delinquent_parcels()


def _fetch_jefferson_raw():
    """Return raw Jefferson delinquent records (no filters) for in-pipeline gate counting."""
    from jefferson_tax_delinquent_api import fetch_delinquent_parcels
    return fetch_delinquent_parcels(district="both")


def _fetch_marshall_raw():
    """Return raw Marshall delinquent records (no filters). Empty while disabled."""
    from marshall_tax_delinquent_api import fetch_delinquent_parcels
    return fetch_delinquent_parcels()


# ── Public API ───────────────────────────────────────────────────────


def fetch_tax_distress(
    *,
    counties: tuple[str, ...] = ("Madison", "Jefferson", "Marshall"),
    individuals_only: bool = False,
    min_balance: float = 0.0,
    stamp_auction_dates: bool = True,
    tiers: tuple[int, ...] | None = (1, 2),
    funnel: FunnelCounter | None = None,
    rate_tracker: ServiceRateTracker | None = None,
) -> tuple[list[NoticeData], FunnelCounter, ServiceRateTracker]:
    """Pull the full tax-distress feed for the requested AL counties.

    Args:
        counties: Counties to query. Defaults to ("Madison", "Jefferson", "Marshall").
            Case-insensitive on input. Anything else is ignored with a warning.
        individuals_only: When True, drop entity-owned parcels (LLC/Inc/Corp
            etc.) via ``config.BUSINESS_RE``. Same semantics as each adapter.
        min_balance: Drop records with ``balance_due`` below this threshold.
            Recommended: 5000 for the high-exposure focus.
        stamp_auction_dates: When True (default), apply the next first-Tuesday
            of May to every tax_sale-typed notice via ``apply_auction_dates``.
        tiers: Tuple of ZIP tiers (1, 2) to keep. ``None`` (or empty tuple)
            disables the filter — returns everything. Default ``(1, 2)``.
        funnel: Optional pre-constructed FunnelCounter. When omitted, a fresh
            FunnelCounter("tax_distress", gates=TAX_DISTRESS_GATES) is built
            internally — pre-seeded with all 5 D-01 gates so the Slack block
            always renders the full sequence.
        rate_tracker: Optional pre-constructed ServiceRateTracker. When
            omitted, a fresh one is built. Threaded into the AssuranceWeb
            Smarty geocode path so the per-call hit-rate surfaces in the
            ``Smarty: X% today | Y% 7-day`` Slack line.

    Returns:
        Tuple of (notices, funnel, rate_tracker). The funnel + tracker are
        returned for ``notify_slack`` / terminal logging in the CLI path.
        Combined NoticeData list across all requested counties, in the
        order Madison → Jefferson → Marshall when all are requested.
    """
    if funnel is None:
        funnel = FunnelCounter("tax_distress", gates=list(TAX_DISTRESS_GATES))
    if rate_tracker is None:
        rate_tracker = ServiceRateTracker()

    selected = {c.strip().title() for c in counties if c}

    # ── Gate 1: bulk_fetched — total raw records across all counties ──
    raw_records: list = []  # list of source records (mixed types)
    raw_by_county: dict[str, list] = {}

    if "Madison" in selected:
        logger.info("Fetching Madison tax-delinquent feed…")
        recs = _fetch_madison_raw()
        raw_by_county["Madison"] = recs
        raw_records.extend(recs)
    if "Jefferson" in selected:
        logger.info("Fetching Jefferson tax-delinquent rosters…")
        recs = _fetch_jefferson_raw()
        raw_by_county["Jefferson"] = recs
        raw_records.extend(recs)
    if "Marshall" in selected:
        logger.info("Fetching Marshall tax-delinquent feed…")
        recs = _fetch_marshall_raw()
        raw_by_county["Marshall"] = recs
        raw_records.extend(recs)

    unknown = selected - {"Madison", "Jefferson", "Marshall"}
    if unknown:
        logger.warning("Unknown counties skipped: %s", sorted(unknown))

    funnel.set("bulk_fetched", len(raw_records))

    # ── Gate 2: individual_owner_filtered ──────────────────────────────
    if individuals_only:
        filtered_indiv: list = []
        for r in raw_records:
            if getattr(r, "is_individual_owner", True):
                filtered_indiv.append(r)
        logger.info(
            "individual_owner filter: %d → %d (dropped %d entities)",
            len(raw_records), len(filtered_indiv), len(raw_records) - len(filtered_indiv),
        )
    else:
        # Pass-through — gate count equals prior gate (D-01 invariant: gate
        # always renders even when no filter applied this run).
        filtered_indiv = list(raw_records)
    funnel.set("individual_owner_filtered", len(filtered_indiv))

    # ── Gate 3: min_balance_filtered ───────────────────────────────────
    if min_balance > 0:
        filtered_bal = [
            r for r in filtered_indiv
            if getattr(r, "balance_due", 0.0) >= min_balance
        ]
        logger.info(
            "min_balance filter (>= $%s): %d → %d (dropped %d)",
            min_balance, len(filtered_indiv), len(filtered_bal),
            len(filtered_indiv) - len(filtered_bal),
        )
    else:
        filtered_bal = list(filtered_indiv)
    funnel.set("min_balance_filtered", len(filtered_bal))

    # ── Convert survivors to NoticeData (county-aware) ────────────────
    # Partition by county so we know which records need the AssuranceWeb
    # Smarty geocode (Madison + Marshall — their bulk feeds lack ZIP).
    madison_set = set(id(r) for r in raw_by_county.get("Madison", []))
    marshall_set = set(id(r) for r in raw_by_county.get("Marshall", []))

    notices: list[NoticeData] = []
    smarty_targets: list[tuple[NoticeData, str]] = []  # (notice, anchor_county)
    for r in filtered_bal:
        if id(r) in madison_set:
            from madison_tax_delinquent_api import to_notice_data as md_to
            n = md_to(r)
            notices.append(n)
            if not n.zip and n.address:
                smarty_targets.append((n, "Madison"))
        elif id(r) in marshall_set:
            from marshall_tax_delinquent_api import to_notice_data as ma_to
            n = ma_to(r)
            notices.append(n)
            if not n.zip and n.address:
                smarty_targets.append((n, "Marshall"))
        else:
            # Jefferson — already has city/zip from the published roster
            from jefferson_tax_delinquent_api import to_notice_data as jf_to
            n = jf_to(r)
            notices.append(n)

    # ── Gate 4: smarty_geocoded — Madison + Marshall ZIP recovery ─────
    # Jefferson records already carry ZIP from their roster, so they count
    # as "geocoded" by default. Per CONTEXT.md D-04, rate_tracker records
    # one outcome per logical Smarty call regardless of the multi-anchor
    # retry inside smarty_zip_for_assuranceweb_address.
    from address_standardizer import (
        smarty_zip_for_madison_address,
        smarty_zip_for_marshall_address,
    )

    smarty_hits = 0
    for n, anchor_county in smarty_targets:
        if anchor_county == "Madison":
            city, zip_code = smarty_zip_for_madison_address(
                n.address, rate_tracker=rate_tracker,
            )
        else:  # Marshall
            city, zip_code = smarty_zip_for_marshall_address(
                n.address, rate_tracker=rate_tracker,
            )
        if zip_code:
            n.zip = zip_code
            if city and not n.city:
                n.city = city
            smarty_hits += 1

    # Records with a resolved ZIP after this stage: Jefferson (always) +
    # Madison/Marshall hits + any Madison/Marshall record that already had
    # a zip from to_notice_data (none today, but defensive).
    geocoded_count = sum(1 for n in notices if n.zip)
    funnel.set("smarty_geocoded", geocoded_count)
    logger.info(
        "smarty_geocoded: %d notices have a resolved ZIP (Madison/Marshall "
        "Smarty hits: %d/%d attempted)",
        geocoded_count, smarty_hits, len(smarty_targets),
    )

    # ── Stamp auction dates BEFORE tier gate (no behavior change) ─────
    if stamp_auction_dates:
        apply_auction_dates(notices)

    # ── Gate 5: tier_gated ─────────────────────────────────────────────
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

    return notices, funnel, rate_tracker


# ── Phase 2: Slack notification ──────────────────────────────────────


def _build_summary_text(
    notices: list[NoticeData],
    funnel: FunnelCounter,
) -> str:
    """Build the markdown header for the tax-distress Slack post.

    Short summary: per-county counts + total balance. Funnel + service
    rates render in the following blocks (D-02 — one message, three blocks).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    by_county: dict[str, list[NoticeData]] = {}
    for n in notices:
        by_county.setdefault(n.county or "(unknown)", []).append(n)
    total_balance = sum(_safe_float(n.tax_delinquent_amount) for n in notices)

    parts = [f"*Tax-Distress Run — {today}*"]
    if notices:
        per_county = ", ".join(
            f"{county}: {len(recs)}"
            for county, recs in sorted(by_county.items())
        )
        parts.append(f"{len(notices)} records ({per_county})")
        parts.append(f"Combined balance: ${total_balance:,.0f}")
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
    """Post the tax-distress run summary to Slack/Discord as a single message.

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
        prog="tax_distress_pipeline",
        description="Unified Madison + Jefferson + Marshall tax-delinquent / tax-sale pull.",
    )
    p.add_argument(
        "--counties", default="Madison,Jefferson,Marshall",
        help="Comma-separated counties (default: Madison,Jefferson,Marshall).",
    )
    p.add_argument(
        "--individuals-only", action="store_true",
        help="Drop entity-owned parcels (LLC/Inc/Corp/etc.).",
    )
    p.add_argument(
        "--min-balance", type=float, default=0.0,
        help="Minimum balance_due to keep (recommended: 5000).",
    )
    p.add_argument(
        "--no-auction-stamp", action="store_true",
        help="Skip Phase 3 auction-date stamping.",
    )
    p.add_argument(
        "--tiers", type=str, default="1,2",
        help="Comma-separated ZIP tiers to keep (default '1,2'). 'all' "
             "disables the filter. Madison/Marshall records now get an "
             "in-pipeline Smarty geocode (smarty_geocoded gate) before the "
             "tier filter, so the tier gate is meaningful for all 3 counties.",
    )
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
    """Pretty-print a per-county summary to stdout."""
    by_county: dict[str, list[NoticeData]] = {}
    for n in notices:
        by_county.setdefault(n.county, []).append(n)

    print(f"\n=== Tax-distress summary — {len(notices)} total notices ===")
    for county, recs in sorted(by_county.items()):
        balance = sum(_safe_float(r.tax_delinquent_amount) for r in recs)
        appraised = sum(_safe_float(r.assessed_value) for r in recs)
        sale_count = sum(1 for r in recs if r.notice_type == "tax_sale")
        delq_count = sum(1 for r in recs if r.notice_type == "tax_delinquent")
        print(f"\n  {county}: {len(recs)} records")
        print(f"    tax_sale:        {sale_count}")
        print(f"    tax_delinquent:  {delq_count}")
        print(f"    Total balance:   ${balance:,.2f}")
        print(f"    Total appraised: ${appraised:,.2f}")
        # Sample auction date (just one record to confirm stamping)
        sample_auction = next((r.auction_date for r in recs if r.auction_date), "")
        if sample_auction:
            print(f"    Auction date:    {sample_auction}")


def _safe_float(s: str) -> float:
    try:
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = _build_argparser().parse_args(argv)

    counties = tuple(c.strip() for c in args.counties.split(",") if c.strip())

    tiers_arg = (args.tiers or "").lower()
    tiers: tuple[int, ...] | None
    if tiers_arg in ("", "all"):
        tiers = None
    else:
        tiers = tuple(int(t) for t in args.tiers.split(",") if t.strip().isdigit())

    notices, funnel, rate_tracker = fetch_tax_distress(
        counties=counties,
        individuals_only=args.individuals_only,
        min_balance=args.min_balance,
        stamp_auction_dates=not args.no_auction_stamp,
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
