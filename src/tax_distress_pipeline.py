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

CLI
====
    # Both counties, full feed
    python src/tax_distress_pipeline.py

    # Phase 1 canonical filter (individuals only, $5k+ owed)
    python src/tax_distress_pipeline.py --individuals-only --min-balance 5000

    # Single county
    python src/tax_distress_pipeline.py --counties Madison

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

logger = logging.getLogger(__name__)


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


def _fetch_madison(*, individuals_only: bool, min_balance: float) -> list[NoticeData]:
    from madison_tax_delinquent_api import fetch_delinquent_parcels, to_notice_data
    recs = fetch_delinquent_parcels(
        individuals_only=individuals_only, min_balance=min_balance,
    )
    return [to_notice_data(r) for r in recs]


def _fetch_jefferson(*, individuals_only: bool, min_balance: float) -> list[NoticeData]:
    from jefferson_tax_delinquent_api import fetch_delinquent_parcels, to_notice_data
    recs = fetch_delinquent_parcels(
        district="both",
        individuals_only=individuals_only,
        min_balance=min_balance,
    )
    return [to_notice_data(r) for r in recs]


def _fetch_marshall(*, individuals_only: bool, min_balance: float) -> list[NoticeData]:
    """Marshall delinquent feed wrapper. Returns [] while the source is offline.

    The Marshall County delinquent-parcels listing was disabled by the county
    as of 2026-05-12 (page shows "Delinquent Parcels listing is currently
    disabled"). The stub adapter in marshall_tax_delinquent_api probes the
    page on each call and returns [] when disabled, so this wrapper is a
    no-op today. Once Marshall re-enables the listing the stub parser will
    be back-filled and this orchestrator wrapper will start contributing
    records without any further changes here.
    """
    from marshall_tax_delinquent_api import fetch_delinquent_parcels, to_notice_data
    recs = fetch_delinquent_parcels(
        individuals_only=individuals_only, min_balance=min_balance,
    )
    return [to_notice_data(r) for r in recs]


# ── Public API ───────────────────────────────────────────────────────


def fetch_tax_distress(
    *,
    counties: tuple[str, ...] = ("Madison", "Jefferson", "Marshall"),
    individuals_only: bool = False,
    min_balance: float = 0.0,
    stamp_auction_dates: bool = True,
    tiers: tuple[int, ...] | None = (1, 2),
) -> list[NoticeData]:
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

            Note: Madison's bulk feed doesn't expose ZIP (the AssuranceWeb
            search response returns only street). Tier filtering on Madison
            records therefore drops them all under the default. Use
            ``distress_proxy_pipeline`` for Smarty-anchored ZIP recovery +
            tier filtering — that's the canonical tier-aware orchestrator
            for this signal. The flag here is for cross-pipeline consistency.

    Returns:
        Combined list of NoticeData across all requested counties, in the
        order Madison → Jefferson → Marshall when all are requested.
    """
    selected = {c.strip().title() for c in counties if c}
    notices: list[NoticeData] = []

    if "Madison" in selected:
        logger.info("Fetching Madison tax-delinquent feed…")
        notices.extend(_fetch_madison(
            individuals_only=individuals_only, min_balance=min_balance,
        ))
    if "Jefferson" in selected:
        logger.info("Fetching Jefferson tax-delinquent rosters…")
        notices.extend(_fetch_jefferson(
            individuals_only=individuals_only, min_balance=min_balance,
        ))
    if "Marshall" in selected:
        logger.info("Fetching Marshall tax-delinquent feed…")
        notices.extend(_fetch_marshall(
            individuals_only=individuals_only, min_balance=min_balance,
        ))

    unknown = selected - {"Madison", "Jefferson", "Marshall"}
    if unknown:
        logger.warning("Unknown counties skipped: %s", sorted(unknown))

    if stamp_auction_dates:
        apply_auction_dates(notices)

    # Tier-ZIP filter
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

    return notices


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
             "disables the filter. Note: Madison records lack ZIP at this "
             "stage — use distress_proxy_pipeline for Smarty-anchored "
             "Madison/Marshall tier filtering.",
    )
    p.add_argument(
        "--output-csv", type=Path, default=None,
        help="Write standard Sift-format CSV to this path.",
    )
    p.add_argument(
        "--output-datasift-csv", type=Path, default=None,
        help="Write DataSift-format CSV (80 cols) to this path.",
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

    notices = fetch_tax_distress(
        counties=counties,
        individuals_only=args.individuals_only,
        min_balance=args.min_balance,
        stamp_auction_dates=not args.no_auction_stamp,
        tiers=tiers,
    )

    _summarize(notices)

    if args.output_csv:
        from data_formatter import write_csv
        path = write_csv(notices, str(args.output_csv))
        print(f"\nWrote Sift CSV: {path}")
    if args.output_datasift_csv:
        from datasift_formatter import write_datasift_csv
        path = write_datasift_csv(notices, str(args.output_datasift_csv))
        print(f"Wrote DataSift CSV: {path}")

    if not notices:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
