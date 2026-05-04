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
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Default Birmingham categories — every Accela enforcement record type
# except "Environmental Batch Record" (low individual-record value).
_DEFAULT_BIRM_CATEGORIES = (
    "housing", "vehicles", "environmental", "zoning", "condemnation",
)


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


# ── Public API ───────────────────────────────────────────────────────


def fetch_code_violations(
    *,
    counties: tuple[str, ...] = ("Madison", "Jefferson"),
    # Birmingham (Jefferson) knobs
    categories: tuple[str, ...] = _DEFAULT_BIRM_CATEGORIES,
    days_back: int = 30,
    max_pages: int = 5,
    enrich_details: bool = False,
    headless: bool = True,
    # Huntsville (Madison) knobs
    min_age_years: int = 0,
    # Shared
    enrich_owner: bool = False,
) -> list[NoticeData]:
    """Pull the full code-violation feed for the requested AL counties.

    Args:
        counties: Counties to query. Case-insensitive on input. Default both.
            ``"Madison"`` → Huntsville Unsafe Buildings;
            ``"Jefferson"`` → Birmingham Accela.
        categories: Birmingham Accela record-type CLI keys to query
            (housing, vehicles, environmental, zoning, condemnation).
            Ignored when Jefferson is not selected.
        days_back: Birmingham search window (default 30).
        max_pages: Birmingham per-category pagination cap (default 5).
        enrich_details: Birmingham only — click each case detail page for
            fees + mailing address. Slow (~3s/case).
        headless: Birmingham only — set False for visible Playwright debug.
        min_age_years: Huntsville only — drop cases newer than N whole years
            (Phase 1 high-conversion subset uses 2).
        enrich_owner: Both counties — opt-in tax-roll address-search to fill
            owner of record (~0.3s/case, ~80% hit rate). When combined with
            Birmingham ``enrich_details``, Jefferson tax-roll fires first;
            Accela detail-page only runs for cases the tax-roll missed.

    Returns:
        Combined ``NoticeData`` list across all requested counties, in the
        order Madison → Jefferson when both are requested.
    """
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

    unknown = selected - {"Madison", "Jefferson"}
    if unknown:
        logger.warning("Unknown counties skipped: %s", sorted(unknown))

    return notices


# ── CLI ──────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="code_violation_pipeline",
        description="Unified Madison (Huntsville) + Jefferson (Birmingham) "
                    "code-enforcement pull.",
    )
    p.add_argument(
        "--counties", default="Madison,Jefferson",
        help="Comma-separated counties (default: Madison,Jefferson).",
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
    # Shared
    p.add_argument(
        "--enrich-owner", action="store_true",
        help="Tax-roll owner enrichment for both counties (~80%% hit rate, "
             "~0.3s/case).",
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

    notices = fetch_code_violations(
        counties=counties,
        categories=categories,
        days_back=args.days_back,
        max_pages=args.max_pages,
        enrich_details=args.enrich_details,
        headless=not args.no_headless,
        min_age_years=args.min_age_years,
        enrich_owner=args.enrich_owner,
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
