"""Path A: capture DataSift UUIDs of today's uploaded probate-universe records.

Solves the reliability gap where SmartSkip's query missed today's fresh records
because they were LIST-ADDS on existing DataSift properties — their `created`
timestamp was old, so `-created` ordering buried them past the fetch cap.

This runs AFTER daily_finalize uploads complete. For each row in today's
probate-universe CSVs (probate, pre_probate, apn_probate, obituary_refresh),
we search DataSift by property address to find the record UUID, then persist
the list to output/observability/today_probate_uuids.json.

SmartSkip step then reads that file and passes UUIDs directly via
--property-uuids-file, bypassing the flaky query entirely.

CLI:
    python scripts/capture_today_probate_uuids.py                 # today's CSVs
    python scripts/capture_today_probate_uuids.py --csv <path>    # specific CSV
    python scripts/capture_today_probate_uuids.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds

logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).parent.parent / "output" / "observability" / "today_probate_uuids.json"

# Only these notice types get UUID-captured for SmartSkip. Foreclosure and
# Code Violation records aren't probate-universe → don't need SmartSkip.
PROBATE_NOTICE_TYPES = frozenset({
    "probate", "pre_probate", "obituary", "obit_refreshed",
})


def _find_property_uuid(street: str, zip5: str) -> str | None:
    """Look up DataSift property UUID by street + zip5.

    Uses ds.find_property_uuid_by_address which builds a paginated index
    on first call (DataSift's ?search= filter is confirmed broken as of
    2026-08-26 diagnostic — silently ignored, always returns same 5 records).
    Subsequent calls are O(1) dict lookups.
    """
    if not (street and zip5):
        return None
    try:
        return ds.find_property_uuid_by_address(street, zip5.strip()[:5])
    except Exception as e:
        logger.debug("Property index lookup failed for %r: %s", street, e)
        return None


def _is_probate_universe_csv(csv_path: Path) -> bool:
    """Check if a CSV contains probate-universe records via row 1 notice_type."""
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f), None)
        if not row:
            return False
        notice_type = (row.get("Notice Type") or "").strip().lower()
        return notice_type in PROBATE_NOTICE_TYPES
    except Exception:
        return False


def capture_uuids_from_csv(csv_path: Path) -> list[dict]:
    """Extract UUIDs for every row in a probate-universe CSV.

    Returns a list of {uuid, street, zip5} dicts. Rows without matches
    are logged as warnings but don't halt processing.
    """
    results = []
    if not csv_path.exists():
        logger.warning("CSV not found: %s", csv_path)
        return results
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            street = (row.get("Property Street Address") or "").strip()
            zip5 = (row.get("Property ZIP Code") or "").strip()[:5]
            if not (street and zip5):
                continue
            uuid = _find_property_uuid(street, zip5)
            if uuid:
                results.append({"uuid": uuid, "street": street, "zip5": zip5})
                logger.debug("  ✅ %s (%s) → %s", street, zip5, uuid[:8])
            else:
                logger.warning("  ❌ no DataSift match for %s (%s)", street, zip5)
    return results


def _today_csvs() -> list[Path]:
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
    ap.add_argument("--csv", type=Path, help="Specific CSV (default: today's CSVs)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log matches without writing JSON output")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    csvs = [args.csv] if args.csv else _today_csvs()
    probate_csvs = [p for p in csvs if _is_probate_universe_csv(p)]

    if not probate_csvs:
        logger.info("No probate-universe CSVs found — nothing to capture.")
        if not args.dry_run:
            # Write empty file so SmartSkip step's read-JSON gate doesn't fail
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_text(json.dumps({"uuids": [], "count": 0}, indent=2))
        return 0

    logger.info("Capturing UUIDs from %d probate-universe CSV(s):", len(probate_csvs))
    for p in probate_csvs:
        logger.info("  · %s", p.name)

    all_records: list[dict] = []
    for csv_path in probate_csvs:
        records = capture_uuids_from_csv(csv_path)
        logger.info("  %s → %d UUIDs captured", csv_path.name, len(records))
        all_records.extend(records)

    # Deduplicate by UUID (same address might appear in multiple CSVs)
    seen = set()
    unique = []
    for r in all_records:
        if r["uuid"] not in seen:
            seen.add(r["uuid"])
            unique.append(r)

    logger.info("Total unique UUIDs captured: %d", len(unique))

    if args.dry_run:
        logger.info("--dry-run: not writing JSON output")
        for r in unique[:10]:
            logger.info("  %s @ %s %s", r["uuid"][:8], r["street"], r["zip5"])
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": date.today().isoformat(),
        "count": len(unique),
        "uuids": [r["uuid"] for r in unique],
        "records": unique,  # includes street/zip for debug
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2))
    logger.info("Wrote %d UUIDs to %s", len(unique), OUTPUT_PATH.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
