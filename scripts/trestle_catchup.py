"""Trestle catch-up: score phones added AFTER the cascade already ran.

The timing problem (operator investigation 2026-09-04):
DataSift runs a daily BULK auto-skip-trace against 145K+ records (unlimited
$97/mo plan). This activity is async and can add phones to records HOURS
or DAYS after our cascade already processed them. Since our cascade calls
_score_phones() only once (at the end of its per-record pass), any phones
added later by DataSift's bulk activity NEVER get Trestle-scored — meaning
they never get Dial First/Second/Third/Fourth/Drop tags.

Impact: filter presets requiring dial-tier tags (02a/02b Call-Ready) miss
these late-arriving phones. Callers see fewer records than the real
call-ready population.

This script closes the gap. Runs daily via GHA cron, ideally AFTER
DataSift's bulk activity completes (~10-11 AM UTC per today's activity log).

Approach:
  1. Paginate DataSift records with has_phones=true
  2. For each record: fetch detail, inventory phones vs. their tag state
  3. Find UNTAGGED phones (no Dial * tag)
  4. Trestle-score each untagged phone (~$0.05/phone)
  5. Apply Dial First/Second/Third/Fourth/Drop tag per score
  6. Rate-limited + budget-capped
  7. Reports per-tier counts + total spend

Cost model:
  - Trestle: $0.05/phone scored
  - Typical run (est.): 500-2,000 untagged phones/day post-DataSift-bulk
  - Daily cost: $25-100 depending on how much new data DataSift adds

CLI:
    python scripts/trestle_catchup.py                          # default
    python scripts/trestle_catchup.py --dry-run                # preview only
    python scripts/trestle_catchup.py --limit 100              # cap at 100 phones
    python scripts/trestle_catchup.py --max-cost-usd 25        # cost cap
    python scripts/trestle_catchup.py --target-zips-only       # restrict to Tier 1/2
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
import phone_validator as pv
import config as cfg
from target_zips import ALL_TARGET as _TIER_1_2_ZIPS

logger = logging.getLogger(__name__)


TRESTLE_PER_PHONE_COST = 0.05

# Dial-tier tag names (must match what cascade writes via _score_phones/
# add_phone_tag). If any of these appears on a phone, we consider it scored.
DIAL_TIER_TAGS_LC = frozenset({
    "dial first", "dial second", "dial third", "dial fourth", "drop",
})


def _phone_tags_lc(phone: dict) -> set[str]:
    """Extract lowercased tag titles from a phone object."""
    out = set()
    for pt in (phone.get("tags") or phone.get("phone_tags") or []):
        title = pt if isinstance(pt, str) else (pt.get("title") or "")
        if title:
            out.add(title.strip().lower())
    return out


def _norm_phone(raw: str) -> str:
    """Normalize phone to 10-digit format (strip country code, punctuation)."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    if len(digits) == 10:
        return digits
    return ""


def _score_and_tag_phone(puuid: str, phone_num: str, api_key: str,
                         dry_run: bool) -> tuple[str | None, bool]:
    """Score one phone via Trestle, apply the Dial-tier tag.

    Returns (tier_name, success_bool). tier_name is None on Trestle error.
    """
    result = pv.call_trestle(phone_num, api_key, add_litigator=False)
    if result.get("error"):
        logger.debug("Trestle error for %s: %s", phone_num, result.get("error"))
        return (None, False)

    score = None
    if isinstance(result.get("activity_score"), (int, float)):
        score = int(result["activity_score"])
    elif isinstance(result.get("phone_intel"), dict):
        score = result["phone_intel"].get("activity_score")
    tier = pv.assign_tier(score, pv.DEFAULT_TIERS)
    if tier == "Unknown":
        return (None, False)

    if dry_run:
        return (tier, True)

    # Apply the tier tag to the phone
    try:
        tier_uuid = ds.phone_tag_uuid(tier, create_if_missing=True)
        if not tier_uuid:
            logger.warning("Could not resolve phone-tag UUID for tier %r", tier)
            return (tier, False)
        ds.add_phone_tag(puuid, phone_num, [tier_uuid])
        return (tier, True)
    except Exception as e:
        logger.debug("add_phone_tag failed for %s/%s: %s", puuid[:8], phone_num, e)
        return (tier, False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Score phones + log tiers without POSTing tags")
    ap.add_argument("--limit", type=int, default=None,
                    help="Safety cap on total phones scored (default: no cap)")
    ap.add_argument("--max-cost-usd", type=float, default=50.0,
                    help="Hard cost cap (default: $50)")
    ap.add_argument("--target-zips-only", action="store_true",
                    help="Restrict to Tier 1/2 records only (recommended for cost control)")
    ap.add_argument("--page-size", type=int, default=500,
                    help="Records per API list call (default: 500)")
    ap.add_argument("--rate-per-sec", type=float, default=3.0,
                    help="Max phone-scoring rate (default: 3/sec)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    if not ds.is_configured():
        logger.error("DATASIFT_API_KEY not set. Cannot proceed.")
        return 1
    api_key = getattr(cfg, "TRESTLE_API_KEY", "")
    if not api_key and not args.dry_run:
        logger.error("TRESTLE_API_KEY not set. Cannot proceed.")
        return 1

    logger.info("═" * 72)
    logger.info("Trestle catch-up: score late-arriving phones from DataSift bulk")
    logger.info("═" * 72)
    logger.info("Dry run:           %s", args.dry_run)
    logger.info("Cost cap:          $%.2f", args.max_cost_usd)
    logger.info("Target-ZIPs only:  %s", args.target_zips_only)
    logger.info("Rate cap:          %.1f phones/sec", args.rate_per_sec)

    tier_counts: dict[str, int] = {}
    total_scored = 0
    total_cost = 0.0
    records_visited = 0
    phones_already_scored = 0
    records_no_phones = 0
    records_off_tier = 0
    detail_fetch_fails = 0
    offset = 0
    min_delay = 1.0 / max(args.rate_per_sec, 0.1)

    while True:
        params = {
            "limit": args.page_size,
            "offset": offset,
            "ordering": "-updated",
        }
        try:
            resp = ds._get("/property/", params)
        except Exception as e:
            logger.error("Page fetch failed (offset=%d): %s", offset, e)
            break
        data = resp.get("data") or resp.get("results") or []
        if not data:
            logger.info("End of records (offset=%d)", offset)
            break

        for row in data:
            uuid = row.get("uuid")
            if not uuid:
                continue

            # Cheap ZIP pre-filter (address in list response)
            if args.target_zips_only:
                addr = row.get("address") or {}
                zip5 = (addr.get("zip5") or addr.get("postal_code") or "").strip()[:5]
                if zip5 not in _TIER_1_2_ZIPS:
                    records_off_tier += 1
                    continue

            # Cheap has_phones pre-filter
            if not row.get("has_phones"):
                records_no_phones += 1
                continue

            # Detail fetch for phone inventory
            try:
                detail = ds.get_property(uuid)
            except Exception as e:
                detail_fetch_fails += 1
                logger.debug("Detail fetch failed for %s: %s", uuid[:8], e)
                continue
            records_visited += 1

            phones = detail.get("phones") or detail.get("owner", {}).get("phones") or []
            for phone in phones:
                if not isinstance(phone, dict):
                    continue
                num_raw = phone.get("number") or phone.get("phone_number") or ""
                num = _norm_phone(num_raw)
                if not num:
                    continue

                existing_tags = _phone_tags_lc(phone)
                if existing_tags & DIAL_TIER_TAGS_LC:
                    phones_already_scored += 1
                    continue

                # Untagged phone — score it
                tier, ok = _score_and_tag_phone(uuid, num, api_key, args.dry_run)
                if tier and ok:
                    total_scored += 1
                    total_cost += TRESTLE_PER_PHONE_COST
                    tier_counts[tier] = tier_counts.get(tier, 0) + 1
                    if args.dry_run:
                        logger.info("  [DRY] would tag %s/%s tier=%s (cost=$%.2f)",
                                    uuid[:8], num, tier, total_cost)
                    elif total_scored % 25 == 0:
                        logger.info("  ...scored %d phones ($%.2f so far)",
                                    total_scored, total_cost)
                    time.sleep(min_delay)

                # Cost/limit gates
                if total_cost >= args.max_cost_usd:
                    logger.info("Hit --max-cost-usd $%.2f, stopping", args.max_cost_usd)
                    _print_summary(records_visited, phones_already_scored,
                                   total_scored, total_cost, tier_counts,
                                   records_no_phones, records_off_tier,
                                   detail_fetch_fails, args.dry_run)
                    return 0
                if args.limit and total_scored >= args.limit:
                    logger.info("Hit --limit %d, stopping", args.limit)
                    _print_summary(records_visited, phones_already_scored,
                                   total_scored, total_cost, tier_counts,
                                   records_no_phones, records_off_tier,
                                   detail_fetch_fails, args.dry_run)
                    return 0

        if len(data) < args.page_size:
            break
        offset += args.page_size

    _print_summary(records_visited, phones_already_scored,
                   total_scored, total_cost, tier_counts,
                   records_no_phones, records_off_tier,
                   detail_fetch_fails, args.dry_run)
    return 0


def _print_summary(records_visited, phones_already_scored, total_scored,
                   total_cost, tier_counts, records_no_phones,
                   records_off_tier, detail_fetch_fails, dry_run):
    logger.info("")
    logger.info("═" * 72)
    logger.info("TRESTLE CATCH-UP SUMMARY")
    logger.info("═" * 72)
    logger.info("Records visited (had phones):         %d", records_visited)
    logger.info("Records skipped (no phones):          %d", records_no_phones)
    logger.info("Records skipped (off-tier):           %d", records_off_tier)
    logger.info("Detail fetch failures:                %d", detail_fetch_fails)
    logger.info("Phones already scored (skipped):      %d", phones_already_scored)
    logger.info("Phones scored + tagged this run:      %d %s",
                total_scored, "(dry-run)" if dry_run else "")
    logger.info("Estimated spend:                      $%.2f", total_cost)
    if tier_counts:
        logger.info("Tier distribution:")
        for tier in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"):
            if tier in tier_counts:
                logger.info("  · %-12s %d", tier, tier_counts[tier])


if __name__ == "__main__":
    sys.exit(main())
