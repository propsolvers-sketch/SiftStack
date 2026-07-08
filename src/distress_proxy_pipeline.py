"""Synthetic distress-proxy pipeline — Madison + Jefferson.

Stopgap for the Huntsville code-enforcement coverage gap (the FOIA path
documented at ``docs/foia/huntsville_code_enforcement_request.md`` takes
weeks to land). Combines existing data sources to build a tier-filtered
list of properties that are *likely* code violators even though we don't
have a direct code-enforcement feed:

  1. Tax-delinquent records with balance ≥ $5K (financial distress signal)
  2. Individual owners only (drop LLC / Corp / partnership owners — those
     have professional management, less likely to let property fall into
     code-violation territory)
  3. ZIP in our Tier 1 ∪ Tier 2 list (focused buying universe)
  4. Madison: one-shot Smarty geocode to recover ZIP (Madison's bulk feed
     lacks situs_zip; same fix used in pre_probate_pipeline)
  5. Jefferson: ABSENTEE-OWNER flag when ``mailing_address != situs_address``
     (a strong indicator the owner doesn't live there, can't easily maintain,
     and is more likely to have code-violation exposure)

Output: NoticeData list with:
  - ``notice_type="tax_delinquent"`` (or ``"tax_sale"`` for Jefferson, which
    publishes the annual auction roster)
  - ``notice_subtype="tier_distress_proxy"`` (or ``"tier_distress_proxy_absentee"``
    for the higher-signal Jefferson absentee subset)
  - All tax-distress tags fire on top of these (``tax_high_exposure``,
    ``individual_owner``, plus tier tags)

Same DataSift CSV + Slack pattern as the other tier-filtered pipelines.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import datasift_formatter
from notice_parser import NoticeData
from address_standardizer import (
    smarty_zip_or_city_estimate_for_madison,
    smarty_zip_or_city_estimate_for_marshall,
)
from pre_probate_pipeline_al import (
    _promote_heir_contacts_to_csv_slots,
    _smarty_zip_for_madison_address,
    _smarty_zip_for_marshall_address,
)
from target_zips import zip_tier

load_dotenv(dotenv_path=Path.home() / "Desktop/SiftStack/.env")

logger = logging.getLogger(__name__)


# ── Fetch + filter ───────────────────────────────────────────────────


def _fetch_jefferson_proxy(
    *,
    min_balance: float,
    target_zips: set[str],
    absentee_only: bool,
) -> list[NoticeData]:
    """Jefferson: pull tax-sale roster, filter to tier ZIPs, optionally
    keep only absentee owners (mailing ≠ situs).

    Jefferson publishes one tax-sale roster per year right before the May
    auction. ``current_al_tax_year()`` returns the year we should query
    AFTER the auction. But during the in-between window (e.g. May 2026 ->
    next data is for the May 2027 auction, not posted until early 2027)
    the most recent year may be the current year minus one or two. We
    walk back up to 3 years to find published data.
    """
    from jefferson_tax_delinquent_api import (
        current_al_tax_year, fetch_delinquent_parcels, to_notice_data,
    )

    recs = []
    year = current_al_tax_year()
    for try_year in (year, year - 1, year - 2):
        recs = fetch_delinquent_parcels(
            district="both", year=try_year,
            individuals_only=True, min_balance=min_balance,
        )
        if recs:
            logger.info("Jefferson tax-sale roster %d: %d records", try_year, len(recs))
            break
        logger.debug("Jefferson year %d: 0 records, trying older", try_year)
    if not recs:
        logger.warning("Jefferson: no tax-sale roster found in last 3 years")
        return []

    notices: list[NoticeData] = []
    absentee_count = 0
    in_tier_count = 0
    for r in recs:
        if not r.situs_zip:
            continue
        if r.situs_zip not in target_zips:
            continue
        in_tier_count += 1

        n = to_notice_data(r)
        is_absentee = (
            r.mailing_address
            and r.situs_address
            and r.mailing_address.strip().upper() != r.situs_address.strip().upper()
        )
        if is_absentee:
            absentee_count += 1
        if absentee_only and not is_absentee:
            continue

        # Re-tag with our proxy subtype so the DataSift formatter / filter
        # presets can target this synthetic subset specifically.
        n.notice_subtype = (
            "tier_distress_proxy_absentee" if is_absentee else "tier_distress_proxy"
        )
        notices.append(n)

    logger.info(
        "Jefferson: %d in-tier (Tier 1+2), %d absentee, %d kept",
        in_tier_count, absentee_count, len(notices),
    )
    return notices


def _fetch_madison_proxy(
    *,
    min_balance: float,
    target_zips: set[str],
) -> list[NoticeData]:
    """Madison: pull tax-delinquent records, Smarty-geocode each to recover
    ZIP (bulk feed lacks city/zip), filter to tier ZIPs. No absentee flag
    available (mailing address not in the bulk feed)."""
    from madison_tax_delinquent_api import fetch_delinquent_parcels, to_notice_data

    recs = fetch_delinquent_parcels(
        individuals_only=True,
        min_balance=min_balance,
    )
    logger.info("Madison tax-delinquent raw: %d high-exposure individual records",
                len(recs))

    notices: list[NoticeData] = []
    geocode_hits = 0
    geocode_misses = 0
    in_tier_count = 0
    for r in recs:
        # Smarty geocode for ZIP (Madison's bulk feed has no city/zip)
        if not r.situs_address:
            continue
        # 3-tuple variant with city-tier centroid fallback (2026-07-08).
        city, zip_code, zip_estimated = smarty_zip_or_city_estimate_for_madison(
            r.situs_address,
        )
        if not zip_code:
            geocode_misses += 1
            continue
        geocode_hits += 1
        if zip_code not in target_zips:
            continue
        in_tier_count += 1

        n = to_notice_data(r)
        n.city = city or n.city
        n.zip = zip_code
        n.notice_subtype = "tier_distress_proxy"
        if zip_estimated:
            existing = n.missing_data_flags or ""
            n.missing_data_flags = (
                f"{existing}|zip_estimated_from_city" if existing
                else "zip_estimated_from_city"
            )
        notices.append(n)

        # Be polite to Smarty — small inter-call delay to avoid rate-limit
        time.sleep(0.05)

    logger.info(
        "Madison: %d geocoded (+%d failed), %d in-tier, %d kept",
        geocode_hits, geocode_misses, in_tier_count, len(notices),
    )
    return notices


def _fetch_marshall_proxy(
    *,
    min_balance: float,
    target_zips: set[str],
) -> list[NoticeData]:
    """Marshall: same flow as Madison (AssuranceWeb platform, no mailing
    address in bulk feed so no absentee flag). Returns [] while Marshall's
    delinquent listing is disabled by the county.
    """
    from marshall_tax_delinquent_api import (
        fetch_delinquent_parcels,
        is_source_disabled,
        to_notice_data,
    )

    if is_source_disabled():
        logger.info(
            "Marshall: tax-delinquent listing currently disabled — skipping. "
            "Will activate automatically once the county re-enables the feed.",
        )
        return []

    recs = fetch_delinquent_parcels(
        individuals_only=True,
        min_balance=min_balance,
    )
    logger.info("Marshall tax-delinquent raw: %d high-exposure individual records",
                len(recs))

    notices: list[NoticeData] = []
    geocode_hits = 0
    geocode_misses = 0
    in_tier_count = 0
    for r in recs:
        if not r.situs_address:
            continue
        # 3-tuple variant with city-tier centroid fallback (2026-07-08).
        city, zip_code, zip_estimated = smarty_zip_or_city_estimate_for_marshall(
            r.situs_address,
        )
        if not zip_code:
            geocode_misses += 1
            continue
        geocode_hits += 1
        if zip_code not in target_zips:
            continue
        in_tier_count += 1

        n = to_notice_data(r)
        n.city = city or n.city
        n.zip = zip_code
        n.notice_subtype = "tier_distress_proxy"
        if zip_estimated:
            existing = n.missing_data_flags or ""
            n.missing_data_flags = (
                f"{existing}|zip_estimated_from_city" if existing
                else "zip_estimated_from_city"
            )
        notices.append(n)

        time.sleep(0.05)

    logger.info(
        "Marshall: %d geocoded (+%d failed), %d in-tier, %d kept",
        geocode_hits, geocode_misses, in_tier_count, len(notices),
    )
    return notices


# ── Public API ───────────────────────────────────────────────────────


def fetch_distress_proxy(
    *,
    counties: tuple[str, ...] = ("Madison", "Jefferson", "Marshall"),
    min_balance: float = 5000.0,
    absentee_only: bool = False,
    target_zips: Optional[set[str]] = None,
) -> list[NoticeData]:
    """Build a tier-filtered synthetic distress-proxy list across AL counties.

    Args:
        counties: Counties to query. Case-insensitive.
        min_balance: Minimum tax-delinquent balance (default $5K — the
            ``tax_high_exposure`` threshold used elsewhere in the pipeline).
        absentee_only: Jefferson only. When True, drop owner-occupied
            properties (mailing == situs) and keep only the absentee subset.
            Strongest signal but cuts volume by ~60%.
        target_zips: Override the ZIP set. Defaults to Tier 1 ∪ Tier 2.

    Returns:
        Combined NoticeData list across selected counties. Each record has
        ``notice_subtype`` set to ``tier_distress_proxy`` (or
        ``tier_distress_proxy_absentee`` for Jefferson absentee).
    """
    if target_zips is None:
        from target_zips import ALL_TARGET
        target_zips = set(ALL_TARGET)

    selected = {c.strip().title() for c in counties if c}
    notices: list[NoticeData] = []

    if "Madison" in selected:
        logger.info("→ Madison distress-proxy pull")
        notices.extend(_fetch_madison_proxy(
            min_balance=min_balance,
            target_zips=target_zips,
        ))
    if "Jefferson" in selected:
        logger.info("→ Jefferson distress-proxy pull")
        notices.extend(_fetch_jefferson_proxy(
            min_balance=min_balance,
            target_zips=target_zips,
            absentee_only=absentee_only,
        ))
    if "Marshall" in selected:
        logger.info("→ Marshall distress-proxy pull")
        notices.extend(_fetch_marshall_proxy(
            min_balance=min_balance,
            target_zips=target_zips,
        ))

    return notices


# ── DataSift CSV + skip-trace ────────────────────────────────────────


def prepare_notices(
    notices: list[NoticeData],
    *,
    skip_trace: bool = False,
) -> dict | None:
    """Optional skip-trace pass. Mutates notices in place; returns stats dict."""
    if not skip_trace or not notices:
        return None
    try:
        import tracerfy_skip_tracer
        stats = tracerfy_skip_tracer.batch_skip_trace(notices)
        logger.info(
            "Skip-trace: submitted=%d matched=%d phones=%d emails=%d cost=$%.2f",
            stats.get("submitted", 0), stats.get("matched", 0),
            stats.get("phones_found", 0), stats.get("emails_found", 0),
            stats.get("cost", 0.0),
        )
        for n in notices:
            _promote_heir_contacts_to_csv_slots(n)
        return stats
    except Exception as e:
        logger.warning("Skip-trace failed: %s", e)
        return {"error": str(e)}


# ── Slack ────────────────────────────────────────────────────────────


def build_slack_message(
    notices: list[NoticeData],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
) -> str:
    """Concise Slack message for the distress-proxy run."""
    total = len(notices)
    today = date.today().strftime("%Y-%m-%d")
    by_county = Counter(n.county for n in notices)
    by_subtype = Counter(n.notice_subtype for n in notices)
    by_tier = Counter(zip_tier(n.zip) for n in notices)

    lines: list[str] = []
    lines.append(f"*Alabama Distress-Proxy — {today}* (FOIA stopgap)")
    lines.append(
        f"  total in-tier: {total}  ·  "
        f"Jefferson: {by_county.get('Jefferson', 0)}  ·  "
        f"Madison: {by_county.get('Madison', 0)}  ·  "
        f"Marshall: {by_county.get('Marshall', 0)}  ·  "
        f"absentee: {by_subtype.get('tier_distress_proxy_absentee', 0)}  ·  "
        f"T1: {by_tier.get(1, 0)}  T2: {by_tier.get(2, 0)}"
    )

    if not notices:
        lines.append("")
        lines.append("_No in-tier distress-proxy records this run._")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"*Top {min(10, total)} by balance owed*")
    sorted_n = sorted(notices,
                      key=lambda n: float(n.tax_delinquent_amount or 0),
                      reverse=True)[:10]
    for n in sorted_n:
        bal = float(n.tax_delinquent_amount or 0)
        val = ""
        try:
            val = f"  ·  est ${float(n.assessed_value):,.0f}" if n.assessed_value else ""
        except (ValueError, TypeError):
            pass
        tier = zip_tier(n.zip)
        county_short = {"Jefferson": "JC", "Madison": "MC", "Marshall": "MA"}.get(
            n.county, "??",
        )
        absentee = " · absentee" if n.notice_subtype == "tier_distress_proxy_absentee" else ""
        addr = (n.address or "?")[:38]
        lines.append(
            f"  · ${bal:>8,.0f}  T{tier}-{county_short}  {addr}, {n.city} {n.zip}{val}{absentee}"
        )
        lines.append(f"      owner: {n.owner_name}")

    if skip_trace_stats:
        sub = skip_trace_stats.get("submitted", 0)
        if sub:
            matched = skip_trace_stats.get("matched", 0)
            ph = skip_trace_stats.get("phones_found", 0)
            em = skip_trace_stats.get("emails_found", 0)
            cost = skip_trace_stats.get("cost", 0.0)
            lines.append("")
            lines.append(
                f"*Skip-trace:* {matched}/{sub} matched · {ph} phones, {em} emails · ${cost:.2f}"
            )

    if csv_path:
        lines.append("")
        lines.append(f"*CSV:* `{csv_path.name}` — {csv_path.parent}")

    return "\n".join(lines)


def notify_slack(
    notices: list[NoticeData],
    csv_path: Optional[Path] = None,
    skip_trace_stats: dict | None = None,
    webhook_url: str | None = None,
) -> bool:
    """Post the distress-proxy run summary to Slack/Discord."""
    import slack_notifier
    text = build_slack_message(notices, csv_path=csv_path,
                                skip_trace_stats=skip_trace_stats)
    sent = slack_notifier._send_webhook(text, webhook_url=webhook_url)
    if sent:
        logger.info("Slack notification sent (%d records)", len(notices))
    else:
        logger.warning("Slack notification failed (no webhook or send error)")
    return sent


# ── Summary + CLI ────────────────────────────────────────────────────


def _print_summary(notices: list[NoticeData]) -> None:
    total = len(notices)
    by_county = Counter(n.county for n in notices)
    by_subtype = Counter(n.notice_subtype for n in notices)
    by_tier = Counter(zip_tier(n.zip) for n in notices)
    by_zip = Counter(n.zip for n in notices)

    print(f"\n{'═' * 64}")
    print(f"  Alabama Distress-Proxy — {total} record(s)")
    print(f"{'═' * 64}")
    print(f"  Jefferson:                   {by_county.get('Jefferson', 0)}")
    print(f"  Madison:                     {by_county.get('Madison', 0)}")
    print(f"  Marshall:                    {by_county.get('Marshall', 0)}")
    print(f"  Tier 1:                      {by_tier.get(1, 0)}")
    print(f"  Tier 2:                      {by_tier.get(2, 0)}")
    print(f"  absentee subtype:            {by_subtype.get('tier_distress_proxy_absentee', 0)}")
    print(f"  occupied subtype:            {by_subtype.get('tier_distress_proxy', 0)}")

    print(f"\n  ZIP distribution:")
    for z, n in by_zip.most_common():
        tier = zip_tier(z)
        print(f"    {z} (T{tier}): {n}")

    if notices:
        sorted_n = sorted(notices,
                          key=lambda n: float(n.tax_delinquent_amount or 0),
                          reverse=True)
        print(f"\n  Top 10 by balance owed:")
        for n in sorted_n[:10]:
            bal = float(n.tax_delinquent_amount or 0)
            val = float(n.assessed_value or 0)
            absentee = " ★absentee" if n.notice_subtype == "tier_distress_proxy_absentee" else ""
            print(f"    ${bal:>9,.0f}  T{zip_tier(n.zip)}-{n.county[:3]}  "
                  f"{n.address[:40]}, {n.city} {n.zip}  est ${val:,.0f}{absentee}")
            print(f"        owner: {n.owner_name}")
    print()


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Build a tier-filtered synthetic distress-proxy list for "
                    "Madison + Jefferson + Marshall — stopgap for the Huntsville FOIA gap.",
    )
    p.add_argument("--counties", default="Madison,Jefferson,Marshall",
                   help="Comma-separated counties (default: Madison,Jefferson,Marshall).")
    p.add_argument("--min-balance", type=float, default=5000.0,
                   help="Min tax-delinquent balance to keep (default 5000).")
    p.add_argument("--absentee-only", action="store_true",
                   help="Jefferson only — keep only absentee owners (mailing != situs).")
    p.add_argument("--datasift-csv", action="store_true",
                   help="Write enriched results to a DataSift CSV.")
    p.add_argument("--skip-trace", action="store_true",
                   help="With --datasift-csv: also run Tracerfy skip-trace.")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post run summary to Slack/Discord.")
    p.add_argument("--json", action="store_true",
                   help="JSON output instead of summary.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "h2", "hpack", "primp", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    counties = tuple(c.strip() for c in args.counties.split(",") if c.strip())

    notices = fetch_distress_proxy(
        counties=counties,
        min_balance=args.min_balance,
        absentee_only=args.absentee_only,
    )

    if args.json:
        payload = [{
            "county": n.county, "address": n.address, "city": n.city, "zip": n.zip,
            "owner_name": n.owner_name, "parcel_id": n.parcel_id,
            "tax_delinquent_amount": n.tax_delinquent_amount,
            "assessed_value": n.assessed_value,
            "notice_subtype": n.notice_subtype,
        } for n in notices]
        print(json.dumps(payload, indent=2))
    else:
        _print_summary(notices)

    csv_path: Optional[Path] = None
    skip_trace_stats: dict | None = None

    if args.datasift_csv and notices:
        if args.skip_trace:
            skip_trace_stats = prepare_notices(notices, skip_trace=True)
        csv_path = datasift_formatter.write_datasift_csv(notices)
        print(f"\n✓ DataSift CSV written: {csv_path}")
    elif args.datasift_csv and not notices:
        print("\n· DataSift CSV: 0 records.")
    elif args.skip_trace:
        print("\n· --skip-trace ignored (requires --datasift-csv).")

    if args.notify_slack:
        sent = notify_slack(notices, csv_path=csv_path,
                            skip_trace_stats=skip_trace_stats)
        if sent:
            print(f"✓ Slack notification posted")
        else:
            print(f"· Slack notification failed (check SLACK_WEBHOOK_URL)")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
