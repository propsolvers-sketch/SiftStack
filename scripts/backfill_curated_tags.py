"""One-time backfill: promote the curated tag set onto historical uploads.

Replays every archived upload CSV in Dropbox/SiftStack/Archives/ through
scripts/apply_subtype_tags.apply_subtypes_from_csv — the same code path
the daily sweep now uses post-upload — so records uploaded BEFORE the
curated-promotion change (2026-09-05) get the same filterable tags.

Why a two-pass index (the lesson from the retired FORECLOSURE backfill):
  * DataSift ignores ?lists= and ?search= on /property/ — both silently
    return records by recency. The ONLY sound way to map a CSV row to a
    UUID is the exact street|zip5 address index.
  * DataSift hard-caps any query at 10,000 rows. One index by -created
    reaches the newest ~10K; older records need a second index by
    -updated (a different 10K window — records recently list-added or
    cascade-tagged). Pass 1 resolves what it can, pass 2 retries the
    misses against the -updated index. Records outside BOTH windows are
    reported as unresolved (typically stale: auctions/hearings long past).

Budget-aware: stops cleanly when ds.budget_remaining() drops under a
floor (DataSift's per-process/day cap surfaced as "budget_exhausted"
during earlier backfills) and prints a resume hint. Idempotent — DataSift
dedupes tag adds, so re-running is safe.

CLI:
    python scripts/backfill_curated_tags.py --dry-run             # preview
    python scripts/backfill_curated_tags.py --since 20260601      # files dated on/after
    python scripts/backfill_curated_tags.py --limit-files 10
    python scripts/backfill_curated_tags.py                       # everything
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
from apply_subtype_tags import apply_subtypes_from_csv

logger = logging.getLogger(__name__)

ARCHIVE_ROOT = "/SiftStack/Archives"
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{8})")
BUDGET_FLOOR = 100


def _file_date(name: str) -> str:
    """YYYYMMDD from an archive filename, or '' if none found."""
    m = _DATE_RE.search(name)
    if not m:
        return ""
    return m.group(1).replace("-", "")


def _list_archives(dbx, since: str) -> list:
    entries = [
        e for e in dbx.files_list_folder(ARCHIVE_ROOT).entries
        if e.name.startswith("datasift_upload_") and e.name.endswith(".csv")
    ]
    if since:
        entries = [e for e in entries if _file_date(e.name) >= since]
    entries.sort(key=lambda e: (_file_date(e.name), e.name))
    return entries


def _write_rows_csv(rows: list[dict], path: Path) -> None:
    """Write dict rows to CSV with the union of keys as header (80-col schema)."""
    header: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                header.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="", help="Only archives dated on/after YYYYMMDD")
    ap.add_argument("--limit-files", type=int, default=None, help="Cap on archive files")
    ap.add_argument("--dry-run", action="store_true", help="Resolve + log, no tag writes")
    ap.add_argument("--skip-pass2", action="store_true",
                    help="Don't retry misses against the -updated index")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )
    logging.getLogger("dropbox").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if not ds.is_configured():
        logger.error("DATASIFT_API_KEY not set. Cannot proceed.")
        return 1

    from dropbox_archive_uploader import _get_client
    dbx = _get_client()
    entries = _list_archives(dbx, args.since)
    if args.limit_files:
        entries = entries[: args.limit_files]
    if not entries:
        logger.info("No archive CSVs matched — nothing to do.")
        return 0

    logger.info("═" * 72)
    logger.info("Curated tag backfill — %d archive CSV(s)%s", len(entries),
                f" since {args.since}" if args.since else "")
    logger.info("Dry run: %s | budget remaining: %d", args.dry_run, ds.budget_remaining())
    logger.info("═" * 72)

    tmp = Path(tempfile.mkdtemp(prefix="curated_backfill_"))
    unresolved: list[dict] = []
    files_done = 0
    p1 = {"rows": 0, "with_tags": 0, "tagged": 0, "tags": 0, "failed": 0}

    # ── Pass 1: -created index ──────────────────────────────────────
    for e in entries:
        if ds.budget_remaining() < BUDGET_FLOOR:
            logger.warning("Budget floor reached (%d left). Stopping before %s. "
                           "Resume tomorrow with --since %s",
                           ds.budget_remaining(), e.name, _file_date(e.name))
            break
        try:
            _, resp = dbx.files_download(e.path_lower)
        except Exception as ex:
            logger.warning("  download failed %s: %s", e.name, ex)
            continue
        local = tmp / e.name
        local.write_bytes(resp.content)
        stats = apply_subtypes_from_csv(local, dry_run=args.dry_run,
                                        ordering="-created", unresolved=unresolved)
        files_done += 1
        p1["rows"] += stats["rows_total"]
        p1["with_tags"] += stats["rows_with_tags"]
        p1["tagged"] += stats["rows_tagged"]
        p1["tags"] += stats["tags_applied"]
        p1["failed"] += stats["rows_failed"]
        logger.info("  %s: rows=%d promotable=%d tagged=%d unresolved_so_far=%d",
                    e.name, stats["rows_total"], stats["rows_with_tags"],
                    stats["rows_tagged"], len(unresolved))

    # ── Pass 2: retry misses against the -updated index ─────────────
    p2 = {"tagged": 0, "tags": 0, "failed": 0}
    still_unresolved: list[dict] = []
    if unresolved and not args.skip_pass2 and ds.budget_remaining() >= BUDGET_FLOOR:
        logger.info("Pass 2: %d unresolved rows → rebuilding index by -updated", len(unresolved))
        ds.build_property_index(ordering="-updated")   # replaces the cached index ONCE
        misses_csv = tmp / "_pass2_unresolved.csv"
        _write_rows_csv(unresolved, misses_csv)
        stats = apply_subtypes_from_csv(misses_csv, dry_run=args.dry_run,
                                        ordering="-updated", unresolved=still_unresolved)
        p2["tagged"] = stats["rows_tagged"]
        p2["tags"] = stats["tags_applied"]
        p2["failed"] = stats["rows_failed"]
    else:
        still_unresolved = unresolved

    logger.info("")
    logger.info("═" * 72)
    logger.info("CURATED BACKFILL SUMMARY%s", " (dry-run)" if args.dry_run else "")
    logger.info("═" * 72)
    logger.info("Archive files processed:      %d / %d", files_done, len(entries))
    logger.info("Rows scanned:                 %d", p1["rows"])
    logger.info("Rows with promotable tags:    %d", p1["with_tags"])
    logger.info("Pass 1 tagged (-created):     %d rows, %d tags", p1["tagged"], p1["tags"])
    logger.info("Pass 2 tagged (-updated):     %d rows, %d tags", p2["tagged"], p2["tags"])
    logger.info("Still unresolved (both idx):  %d rows", len(still_unresolved))
    logger.info("Budget remaining:             %d", ds.budget_remaining())
    if still_unresolved:
        keep = tmp / "_unresolved_final.csv"
        _write_rows_csv(still_unresolved, keep)
        logger.info("Unresolved rows written to %s", keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
