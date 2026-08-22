"""Apply high-signal subtype tags to freshly-uploaded records.

The DataSift wizard's Custom Tags input at Step 3 is unreliable — verified
2026-07-20 that per-record subtype tags (foreclosure_cancelled,
foreclosure_postponed, probate_sale, probate_final_settlement) get dropped
silently even when passed via `extra_custom_tags`. Notes DO reflect the
subtype (via the Notes column) but the operator can't filter/sort DataSift
by note text — needs actual tag membership.

This script runs AFTER daily_finalize.py's uploads complete. For each row
in each uploaded CSV, if `Probate Subtype` is in the promotable set, we:

  1. Search DataSift by property address to find the record UUID
  2. Apply the subtype as a record tag via ds.add_tags (title-based —
     matches the mark_vendor_traced pattern that we know works)

Runs cheap: 1 GET (property search) + 1 POST (add_tags) per matching record.
Typical daily flow: 5-15 records with promotable subtype = ~30 sec runtime.

Wiring: called from daily_finalize.py's --upload-only mode right after
uploads. Also runnable as a standalone CLI for backfill:

    python scripts/apply_subtype_tags.py                    # today's CSVs
    python scripts/apply_subtype_tags.py --csv output/leads/<file>.csv
    python scripts/apply_subtype_tags.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds

logger = logging.getLogger(__name__)


# Only these subtypes get promoted to record tags — high-signal for calling
# strategy. Everything else stays in Notes only.
PROMOTABLE_SUBTYPES = frozenset({
    "foreclosure_cancelled",
    "foreclosure_postponed",
    "probate_sale",
    "probate_final_settlement",
})


def _find_property_uuid(street: str, zip5: str) -> str | None:
    """Address-based DataSift search — matches on street + zip5."""
    if not (street and zip5):
        return None
    zip5 = zip5.strip()[:5]
    try:
        resp = ds._get("/property/", {"search": street, "limit": 25})
        for cand in (resp.get("data") or []):
            addr = cand.get("address") or {}
            cand_street = (addr.get("street") or "").strip().lower()
            cand_zip = (addr.get("zip5") or addr.get("postal_code") or "").strip()[:5]
            if cand_street == street.strip().lower() and cand_zip == zip5:
                return cand.get("uuid")
    except Exception as e:
        logger.debug("Property search failed for %r: %s", street, e)
    return None


def apply_subtypes_from_csv(csv_path: Path, *, dry_run: bool = False) -> dict:
    """Read a CSV, apply subtype tags for any row with a promotable subtype."""
    stats = {
        "csv": csv_path.name,
        "rows_total": 0,
        "rows_with_subtype": 0,
        "rows_matched_uuid": 0,
        "rows_tagged": 0,
        "rows_failed": 0,
    }
    if not csv_path.exists():
        logger.warning("CSV not found: %s", csv_path)
        return stats

    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["rows_total"] += 1
            subtype = (row.get("Probate Subtype") or "").strip().lower()
            if subtype not in PROMOTABLE_SUBTYPES:
                continue
            stats["rows_with_subtype"] += 1

            street = (row.get("Property Street Address") or "").strip()
            zip5 = (row.get("Property ZIP Code") or "").strip()[:5]
            uuid = _find_property_uuid(street, zip5)
            if not uuid:
                stats["rows_failed"] += 1
                logger.warning(
                    "  ❌ no DataSift match for %s (subtype=%s)",
                    street, subtype,
                )
                continue
            stats["rows_matched_uuid"] += 1

            if dry_run:
                logger.info(
                    "  [DRY] would tag %s (%s) with '%s'",
                    uuid[:8], street[:40], subtype,
                )
                continue

            # Ensure tag exists in DataSift (idempotent) + POST title directly
            # (bypasses UUID→title reverse lookup — same fix as mark_vendor_traced).
            try:
                ds.tag_uuid(subtype, create_if_missing=True)
                ds._post(
                    f"/property/{uuid}/add-tags/",
                    {"tags": [subtype]},
                )
                stats["rows_tagged"] += 1
                logger.info("  ✅ tagged %s (%s) with '%s'",
                            uuid[:8], street[:40], subtype)
            except Exception as e:
                stats["rows_failed"] += 1
                logger.warning("  ❌ tag apply failed for %s: %s", uuid[:8], e)

    return stats


def _today_csvs() -> list[Path]:
    """Find today's upload CSVs in output/leads/."""
    leads_dir = Path(__file__).parent.parent / "output" / "leads"
    if not leads_dir.exists():
        return []
    today = date.today().strftime("%Y-%m-%d")
    today_alt = date.today().strftime("%Y%m%d")
    return sorted(
        p for p in leads_dir.glob("datasift_upload_*.csv")
        if today in p.name or today_alt in p.name
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, help="Specific CSV to process (default: today's CSVs)")
    ap.add_argument("--dry-run", action="store_true", help="Log matches, don't write tags")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    csvs = [args.csv] if args.csv else _today_csvs()
    if not csvs:
        logger.info("No CSVs found — nothing to do.")
        return 0

    logger.info("Processing %d CSV(s) for subtype tag promotion", len(csvs))
    grand_total_tagged = 0
    grand_total_failed = 0
    for csv_path in csvs:
        stats = apply_subtypes_from_csv(csv_path, dry_run=args.dry_run)
        if stats["rows_with_subtype"] > 0:
            logger.info(
                "  %s → %d/%d rows with promotable subtype tagged "
                "(%d matched UUID, %d failed)",
                csv_path.name, stats["rows_tagged"],
                stats["rows_with_subtype"], stats["rows_matched_uuid"],
                stats["rows_failed"],
            )
        grand_total_tagged += stats["rows_tagged"]
        grand_total_failed += stats["rows_failed"]

    logger.info(
        "Subtype tag promotion complete: %d records tagged, %d failed",
        grand_total_tagged, grand_total_failed,
    )
    return 0 if grand_total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
