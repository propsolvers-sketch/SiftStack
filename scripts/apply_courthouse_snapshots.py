"""Apply Courthouse Snapshot notes to freshly-uploaded probate records.

Runs AFTER daily_finalize.py's DataSift uploads complete. For each probate-
universe CSV that was uploaded, we:

  1. Read the CSV rows (each = one NoticeData that was uploaded)
  2. Query DataSift by property address to find the matched record UUID
  3. Format a Courthouse Snapshot note via datasift_formatter.format_courthouse_snapshot()
  4. Post the note via ds.add_notes(uuid, snapshot)

Cost: 1 GET + 1 add_notes per record with probate metadata (typically 5-20
records/day across all probate pipelines). Skips records with no probate
metadata (nothing to snapshot).

Wiring: add a step to .github/workflows/daily-sweep.yml after the
"Consolidate CSVs + upload to DataSift" step that runs this script.

CLI:
    # All probate CSVs from output/leads/ produced today
    python scripts/apply_courthouse_snapshots.py

    # Specific CSV
    python scripts/apply_courthouse_snapshots.py --csv output/leads/datasift_upload_probate_ts.csv

    # Dry-run (no writes)
    python scripts/apply_courthouse_snapshots.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
import datasift_formatter as df
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# CSV columns → NoticeData attributes we need for the snapshot.
# Note: datasift_formatter.write_datasift_csv writes 80 columns per notice;
# we only extract the fields the snapshot uses. If the CSV lacks a column,
# that field stays empty on the NoticeData.
_CSV_TO_NOTICE_FIELDS = {
    "Property Street Address": "address",
    "Property City":            "city",
    "Property State":           "state",
    "Property Zip":             "zip",
    "Notice Type":              "notice_type",
    "County":                   "county",
    "Probate Case Number":      "case_number",
    "Judge of Probate":         "judge_name",
    "Probate Subtype":          "notice_subtype",
    "Probate Open Date":        "granted_date",
    "Creditor Claim Deadline":  "creditor_deadline",
    "Petition Filed Date":      "petition_filed_date",
    "Hearing Date":             "hearing_date",
    "Decedent Name":            "decedent_name",
    "Date of Death":            "date_of_death",
    "Owner Deceased":           "owner_deceased",
    "Personal Representative":  "owner_name",
    "Obituary URL":             "obituary_url",
    "Source URL":               "source_url",
    "Parcel ID":                "parcel_id",
    "Total Estate Value":       "total_estate_value",
}


def _reconstruct_notice(row: dict[str, str]) -> NoticeData:
    """Build a partial NoticeData from a CSV row for snapshot formatting."""
    kwargs = {}
    for csv_col, nd_attr in _CSV_TO_NOTICE_FIELDS.items():
        val = (row.get(csv_col) or "").strip()
        if val:
            kwargs[nd_attr] = val
    # `notice_type` + a few required fields default to "" — that's fine
    if "notice_type" not in kwargs:
        kwargs["notice_type"] = "probate"
    # date_added is required by NoticeData constructor
    kwargs.setdefault("date_added", time.strftime("%Y-%m-%d"))
    return NoticeData(**kwargs)


def _find_property_uuid(notice: NoticeData) -> str | None:
    """Look up DataSift property UUID by notice's street + zip5.

    2026-08-26: Rewrote to use ds.find_property_uuid_by_address (paginated
    index) instead of ?search= which was silently broken. The `search`
    parameter on DataSift's /property/ endpoint is IGNORED — always
    returned the same 5 records regardless of query. This script has been
    silently failing to write Courthouse Snapshots since… probably forever.
    """
    street = (notice.address or "").strip()
    zip5 = (notice.zip or "").strip()[:5]
    if not (street and zip5):
        return None
    try:
        return ds.find_property_uuid_by_address(street, zip5)
    except Exception as e:
        logger.debug("Property index lookup failed for %r: %s", street, e)
        return None


def apply_snapshots_to_csv(csv_path: Path, *, dry_run: bool = False) -> dict:
    """Apply Courthouse Snapshot notes to all records in a CSV."""
    stats = {
        "csv": csv_path.name,
        "rows_total": 0,
        "rows_with_probate_data": 0,
        "matched_to_record": 0,
        "notes_written": 0,
        "match_failures": 0,
        "empty_snapshots": 0,
    }
    if not csv_path.exists():
        logger.warning("CSV not found: %s", csv_path)
        return stats

    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["rows_total"] += 1
            notice = _reconstruct_notice(row)
            snapshot = df.format_courthouse_snapshot(notice)
            if not snapshot:
                stats["empty_snapshots"] += 1
                continue
            stats["rows_with_probate_data"] += 1

            uuid = _find_property_uuid(notice)
            if not uuid:
                stats["match_failures"] += 1
                logger.debug("No property match for %s %s", notice.address, notice.zip)
                continue
            stats["matched_to_record"] += 1

            if dry_run:
                logger.info("[DRY] Would write snapshot to %s (%s %s)",
                            uuid[:8], notice.address, notice.zip)
                continue

            try:
                ds.add_notes(uuid, "\n\n" + snapshot)
                stats["notes_written"] += 1
                logger.info("Wrote snapshot → %s (%s)", uuid[:8], notice.address[:40])
            except Exception as e:
                logger.warning("add_notes failed for %s: %s", uuid[:8], e)

    return stats


def _default_csv_paths() -> list[Path]:
    """All probate CSVs from output/leads/ produced in the current run."""
    leads_dir = Path(__file__).parent.parent / "output" / "leads"
    if not leads_dir.exists():
        return []
    # Match probate + pre_probate CSVs (obit-driven and courthouse-driven)
    patterns = [
        "datasift_upload_probate*.csv",
        "datasift_upload_pre_probate*.csv",
        "datasift_upload_obit*.csv",
    ]
    seen = set()
    result = []
    for pattern in patterns:
        for p in leads_dir.glob(pattern):
            if p not in seen:
                seen.add(p)
                result.append(p)
    return sorted(result)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path,
                    help="Specific CSV to process (default: all probate CSVs "
                         "in output/leads/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log what would be written without touching DataSift")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    csvs = [args.csv] if args.csv else _default_csv_paths()
    if not csvs:
        logger.info("No probate CSVs found to process. Exiting cleanly.")
        return 0

    logger.info("Processing %d CSV(s)", len(csvs))
    all_stats = []
    for csv_path in csvs:
        logger.info("── %s ──", csv_path.name)
        stats = apply_snapshots_to_csv(csv_path, dry_run=args.dry_run)
        all_stats.append(stats)
        logger.info(
            "  rows=%d  with_probate=%d  matched=%d  notes_written=%d  "
            "no_match=%d  empty_snapshots=%d",
            stats["rows_total"], stats["rows_with_probate_data"],
            stats["matched_to_record"], stats["notes_written"],
            stats["match_failures"], stats["empty_snapshots"],
        )

    total_written = sum(s["notes_written"] for s in all_stats)
    total_no_match = sum(s["match_failures"] for s in all_stats)
    logger.info("")
    logger.info("═" * 60)
    logger.info(f"  TOTAL notes written: {total_written}")
    if total_no_match:
        logger.info(f"  Total no-match (record not yet in DataSift): {total_no_match}")
        logger.info(f"    → run again after DataSift upload settles, or check")
        logger.info(f"      whether the record's address matches the CSV")
    logger.info("═" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
