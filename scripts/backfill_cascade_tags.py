"""One-time backfill: add traced_tracerfy + traced_datasift tags to records
where phones exist but cascade tags are missing.

Rationale (operator investigation 2026-09-04):
DataSift runs a bulk auto-skip-trace daily against ALL 145K+ records in
the account (per activity log: 9,790 fresh + $17K saved via dedup). This
bulk activity adds phones but doesn't fire our cascade's tag-write code.
Meanwhile our cascade only processes ~200 records/day (Tier 1/2 filter).
The gap: many records have phones from DataSift's bulk skip but never got
our traced_datasift tag because the cascade didn't touch them. Also many
older records ran through Tracerfy in the adapter (which tags upstream
as SiftStack-marked) but haven't been re-processed by cascade.

Detection signal (updated 2026-09-04):
  - has_phones = true → SOME vendor added phones (DataSift bulk / Tracerfy /
    SmartSkip). Truthful evidence that skip-trace occurred.
  - Courthouse Data tag → SiftStack record (adapter ran Tracerfy upstream)

Approach:
  1. Paginate /property/ by -created (newest first)
  2. For each record with has_phones = true:
     - Add traced_tracerfy (Tracerfy runs upstream in every SiftStack adapter)
     - Add traced_datasift (DataSift's bulk auto-skip covers records with phones)
  3. Rate-limit to 5/sec
  4. Idempotent (DataSift dedupes tag adds)
  5. Reports processed/tagged/skipped/errors

Safe to re-run — already-tagged records no-op.

NOTE: last_skip_traced field was found unreliable — DataSift's bulk activity
doesn't consistently update it. Switched to has_phones as the truth signal.

CLI:
    python scripts/backfill_cascade_tags.py                       # default since 2026-06-26
    python scripts/backfill_cascade_tags.py --since 2026-01-01    # further back
    python scripts/backfill_cascade_tags.py --dry-run             # preview only
    python scripts/backfill_cascade_tags.py --limit 10            # test on 10 records
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
from target_zips import ALL_TARGET as _TIER_1_2_ZIPS

logger = logging.getLogger(__name__)


# Two-tier tagging per operator directive 2026-09-04:
# - traced_datasift: applied to ALL records with phones (DataSift's $97/mo
#   unlimited plan auto-skip-traces every record; 145K/day per activity log)
# - traced_tracerfy: applied ONLY to Tier 1/2 records with phones (Tracerfy
#   is a TARGETED vendor that runs in the adapter for high-priority ZIPs only)
TAG_DATASIFT = "traced_datasift"
TAG_TRACERFY = "traced_tracerfy"


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse DataSift's timestamp — accepts multiple ISO variants + date-only."""
    if not raw:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw.replace("+00:00", ""), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Log what would be tagged without POSTing")
    ap.add_argument("--limit", type=int, default=None,
                    help="Safety cap on records to tag (default: no cap)")
    ap.add_argument("--rate-per-sec", type=float, default=5.0,
                    help="Max POST rate (default: 5/sec)")
    ap.add_argument("--page-size", type=int, default=500,
                    help="Records per API list call (default: 500)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    if not ds.is_configured():
        logger.error("DATASIFT_API_KEY not set. Cannot proceed.")
        return 1

    logger.info("═" * 72)
    logger.info("Cascade tag backfill (two-tier scoping)")
    logger.info("═" * 72)
    logger.info("traced_datasift: all records with has_phones=true (DataSift bulk unlimited)")
    logger.info("traced_tracerfy: only records with has_phones + Tier 1/2 ZIP (targeted vendor)")
    logger.info("Dry run:         %s", args.dry_run)
    logger.info("Rate cap:        %.1f records/sec", args.rate_per_sec)
    logger.info("Tier 1/2 ZIPs:   %d codes", len(_TIER_1_2_ZIPS))

    # Resolve tag UUIDs (create if missing — they should already exist from cascade runs)
    if not args.dry_run:
        tag_uuids = {}
        for tag_title in (TAG_DATASIFT, TAG_TRACERFY):
            uuid = ds.tag_uuid(tag_title, create_if_missing=True)
            if not uuid:
                logger.error("Could not resolve/create tag %r", tag_title)
                return 1
            tag_uuids[tag_title] = uuid
            logger.info("  %s UUID: %s", tag_title, uuid[:8])

    # Paginate — newest records first
    logger.info("Paginating /property/ records...")
    tagged_ds_only = 0
    tagged_both = 0
    skipped_no_phones = 0
    errors = 0
    processed = 0
    offset = 0
    min_delay = 1.0 / max(args.rate_per_sec, 0.1)

    while True:
        params = {
            "limit": args.page_size,
            "offset": offset,
            "ordering": "-created",
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
            processed += 1
            uuid = row.get("uuid")
            if not uuid:
                continue

            # Two-tier tagging (per operator scoping directive 2026-09-04):
            # - has_phones=true → traced_datasift (DataSift's bulk auto-skip
            #   covers 100% of the account per activity log)
            # - has_phones=true AND situs zip in Tier 1/2 → also traced_tracerfy
            #   (Tracerfy is targeted to high-priority ZIPs only, runs upstream
            #    in adapter code before upload)
            has_phones = bool(row.get("has_phones"))
            if not has_phones:
                skipped_no_phones += 1
                continue

            # Situs zip from list-endpoint address field
            addr = row.get("address") or {}
            zip5 = (addr.get("zip5") or addr.get("postal_code") or "").strip()[:5]
            in_tier = zip5 in _TIER_1_2_ZIPS

            tags_to_apply = [TAG_DATASIFT]
            if in_tier:
                tags_to_apply.append(TAG_TRACERFY)

            if args.dry_run:
                logger.info("  [DRY] %s zip=%s in_tier=%s → tags=%s",
                            uuid[:8], zip5 or "?", in_tier, tags_to_apply)
                if in_tier:
                    tagged_both += 1
                else:
                    tagged_ds_only += 1
            else:
                try:
                    uuids_to_add = [tag_uuids[t] for t in tags_to_apply]
                    ds.add_tags(uuid, uuids_to_add)
                    if in_tier:
                        tagged_both += 1
                    else:
                        tagged_ds_only += 1
                    total = tagged_both + tagged_ds_only
                    if total % 50 == 0:
                        logger.info("  ...tagged %d records so far (%d in-tier, %d off-tier)",
                                    total, tagged_both, tagged_ds_only)
                except Exception as e:
                    errors += 1
                    logger.warning("  add-tags failed for %s: %s", uuid[:8], e)
                time.sleep(min_delay)

            if args.limit and (tagged_both + tagged_ds_only + errors) >= args.limit:
                logger.info("Hit --limit %d, stopping", args.limit)
                _print_summary(processed, tagged_both, tagged_ds_only,
                               skipped_no_phones, errors, args.dry_run)
                return 0

        if len(data) < args.page_size:
            break
        offset += args.page_size

    _print_summary(processed, tagged_both, tagged_ds_only,
                   skipped_no_phones, errors, args.dry_run)
    return 0 if errors == 0 else 1


def _print_summary(processed, tagged_both, tagged_ds_only, skipped_no_phones, errors, dry_run):
    logger.info("")
    logger.info("═" * 72)
    logger.info("BACKFILL SUMMARY")
    logger.info("═" * 72)
    logger.info("Processed:                              %d records", processed)
    logger.info("Tagged (in Tier 1/2, both tags):        %d %s",
                tagged_both, "(dry-run)" if dry_run else "")
    logger.info("Tagged (off-tier, datasift only):       %d %s",
                tagged_ds_only, "(dry-run)" if dry_run else "")
    logger.info("Skipped (no phones):                    %d", skipped_no_phones)
    logger.info("Errors:                                 %d", errors)


if __name__ == "__main__":
    sys.exit(main())
