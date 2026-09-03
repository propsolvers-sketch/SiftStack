"""One-time backfill: add FORECLOSURE tag to every record in the DataSift
Foreclosure list created since 2026-06-01.

Rationale (operator directive 2026-09-02):
DataSift's filter-preset engine is fundamentally OR across lists, not AND.
To surface records that appear in BOTH the Foreclosure list AND a probate-
universe list (Probate / Pre-Probate / Obituary / etc.), we need a tag
proxy on the foreclosure side that presets can require alongside a probate-
list membership filter.

Going forward, `src/datasift_formatter.py:_build_tags()` auto-appends
FORECLOSURE to every foreclosure record at upload time (commit landing
alongside this script). This script backfills pre-existing records.

Approach:
  1. Resolve Foreclosure list UUID via ds.list_uuid("Foreclosure")
  2. Resolve/create FORECLOSURE tag UUID via ds.tag_uuid("FORECLOSURE",
     create_if_missing=True)
  3. Paginate /property/?lists=<foreclosure_uuid>&ordering=-created
  4. For each record with created >= 2026-06-01, POST add-tags
  5. Rate-limit to ~5 records/sec to be gentle on the API
  6. Report tagged count + skipped count + error count

Safe to re-run — DataSift dedupes tag adds, so already-tagged records
just no-op (add_tags returns 200 with no state change).

CLI:
    python scripts/backfill_foreclosure_tag.py                  # default: since 2026-06-01
    python scripts/backfill_foreclosure_tag.py --since 2026-01-01  # further back
    python scripts/backfill_foreclosure_tag.py --dry-run        # preview only
    python scripts/backfill_foreclosure_tag.py --limit 10       # test on 10 records
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

logger = logging.getLogger(__name__)


FORECLOSURE_LIST_TITLE = "Foreclosure"
FORECLOSURE_TAG_TITLE = "FORECLOSURE"
DEFAULT_SINCE_DATE = "2026-06-01"


def _parse_created_ts(raw: str) -> datetime | None:
    """Parse DataSift's created timestamp — accepts ISO 8601 with or without Z."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw.replace("+00:00", ""), fmt)
        except ValueError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=DEFAULT_SINCE_DATE,
                    help=f"Only tag records created on/after YYYY-MM-DD "
                         f"(default: {DEFAULT_SINCE_DATE})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log what would be tagged without POSTing")
    ap.add_argument("--limit", type=int, default=None,
                    help="Safety cap on records to tag (default: no cap)")
    ap.add_argument("--rate-per-sec", type=float, default=5.0,
                    help="Max POST rate to be gentle on the API (default: 5/sec)")
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

    since_dt = datetime.strptime(args.since, "%Y-%m-%d")
    logger.info("═" * 72)
    logger.info("Foreclosure → FORECLOSURE tag backfill")
    logger.info("═" * 72)
    logger.info("Since date: %s", args.since)
    logger.info("Dry run:    %s", args.dry_run)
    logger.info("Rate cap:   %.1f records/sec", args.rate_per_sec)

    # Resolve list + tag UUIDs
    logger.info("Resolving Foreclosure list UUID...")
    list_uuid = ds.list_uuid(FORECLOSURE_LIST_TITLE, create_if_missing=False)
    if not list_uuid:
        logger.error("List %r not found in DataSift", FORECLOSURE_LIST_TITLE)
        return 1
    logger.info("  Foreclosure list UUID: %s", list_uuid[:8])

    if not args.dry_run:
        logger.info("Resolving/creating FORECLOSURE tag UUID...")
        tag_uuid = ds.tag_uuid(FORECLOSURE_TAG_TITLE, create_if_missing=True)
        if not tag_uuid:
            logger.error("Could not resolve or create tag %r", FORECLOSURE_TAG_TITLE)
            return 1
        logger.info("  FORECLOSURE tag UUID: %s", tag_uuid[:8])
    else:
        tag_uuid = "dry-run-placeholder"

    # Paginate through Foreclosure list — newest first
    logger.info("Paginating Foreclosure list records...")
    tagged = 0
    skipped_old = 0
    errors = 0
    processed = 0
    offset = 0
    min_delay = 1.0 / max(args.rate_per_sec, 0.1)

    while True:
        params = {
            "lists": list_uuid,
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
            logger.info("End of list (offset=%d)", offset)
            break

        for row in data:
            processed += 1
            uuid = row.get("uuid")
            if not uuid:
                continue
            created = _parse_created_ts(row.get("created") or "")
            if created and created < since_dt:
                # -created ordering means once we hit a too-old record,
                # everything after is also too old → we can stop
                skipped_old += 1
                logger.info(
                    "Reached records older than %s (uuid=%s, created=%s) — "
                    "stopping (all remaining will be pre-cutoff)",
                    args.since, uuid[:8], created.date(),
                )
                # Print summary + exit
                _print_summary(tagged, skipped_old, errors, processed, args.dry_run)
                return 0

            if args.dry_run:
                logger.info("  [DRY] would tag %s (created=%s)",
                            uuid[:8], created.date() if created else "?")
                tagged += 1
            else:
                try:
                    ds.add_tags(uuid, [tag_uuid])
                    tagged += 1
                    if tagged % 25 == 0:
                        logger.info("  ...tagged %d records so far", tagged)
                except Exception as e:
                    errors += 1
                    logger.warning("  add-tags failed for %s: %s", uuid[:8], e)
                # Rate limit
                time.sleep(min_delay)

            if args.limit and (tagged + errors) >= args.limit:
                logger.info("Hit --limit %d, stopping", args.limit)
                _print_summary(tagged, skipped_old, errors, processed, args.dry_run)
                return 0

        if len(data) < args.page_size:
            break
        offset += args.page_size

    _print_summary(tagged, skipped_old, errors, processed, args.dry_run)
    return 0 if errors == 0 else 1


def _print_summary(tagged: int, skipped_old: int, errors: int,
                   processed: int, dry_run: bool) -> None:
    logger.info("")
    logger.info("═" * 72)
    logger.info("BACKFILL SUMMARY")
    logger.info("═" * 72)
    logger.info("Processed:       %d records", processed)
    logger.info("Tagged:          %d %s", tagged, "(dry-run)" if dry_run else "")
    logger.info("Skipped as old:  %d", skipped_old)
    logger.info("Errors:          %d", errors)


if __name__ == "__main__":
    sys.exit(main())
