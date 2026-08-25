"""Detect records that CAUGHT UP on their delinquent property taxes.

Runs weekly AFTER tax_distress_pipeline scrapes the current delinquent
roll. Compares:
  · Current parcels in the delinquent list (freshly scraped)
  · DataSift records currently tagged `tax_delinquent`

For records tagged `tax_delinquent` whose parcel_id is NO LONGER in the
current list → they paid off → apply `tax_caught_up` + remove
`tax_delinquent` so tax-distress marketing stops.

The upshot: operator's "Currently Tax Delinquent" filter preset stays
CURRENT — records that catch up drop out of it automatically.

CLI:
    python scripts/detect_tax_caught_up.py                  # weekly cron
    python scripts/detect_tax_caught_up.py --dry-run        # log-only
    python scripts/detect_tax_caught_up.py -v               # verbose

Preconditions:
  · tax_distress_pipeline.py has run and produced a CSV in output/leads/
  · OR pass --current-parcels-file <path> with a JSON list of currently-
    delinquent parcel_ids for direct-injection use
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
import vendor_tags as vt

logger = logging.getLogger(__name__)

REPO = Path(__file__).parent.parent
LEADS_DIR = REPO / "output" / "leads"


def _load_current_parcels_from_csvs() -> set[str]:
    """Read every recent tax_distress CSV and collect parcel_ids.

    Tax-distress CSVs are named datasift_upload_tax_distress_*.csv per the
    tax_distress_pipeline output naming convention.
    """
    parcels: set[str] = set()
    if not LEADS_DIR.exists():
        return parcels
    from datetime import date, timedelta
    today = date.today()
    valid_days = {(today - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(7)}
    valid_days |= {(today - timedelta(days=n)).strftime("%Y%m%d") for n in range(7)}

    csvs = sorted(
        p for p in LEADS_DIR.glob("datasift_upload_tax_distress_*.csv")
        if any(d in p.name for d in valid_days)
    )
    for csv_path in csvs:
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    parcel = (row.get("Parcel ID") or "").strip()
                    if parcel:
                        parcels.add(parcel.upper())
        except Exception as e:
            logger.warning("Failed to read %s: %s", csv_path.name, e)
    return parcels


def _query_tax_delinquent_records() -> list[dict]:
    """Return all DataSift records currently tagged `tax_delinquent`.

    Uses the same paginate + client-side-filter pattern proven in
    query_probate_universe_records (server tag filter is unreliable).
    """
    tag_lc = vt.RECORD_TAG_TAX_DELINQUENT.lower()
    caught_up_lc = vt.RECORD_TAG_TAX_CAUGHT_UP.lower()

    matches: list[dict] = []
    offset = 0
    limit = 500
    MAX_SCAN = 5000  # safety cap

    while offset < MAX_SCAN:
        resp = ds._get("/property/", {
            "limit": limit, "offset": offset, "ordering": "-created",
        })
        page = resp.get("data") or resp.get("results") or []
        if not page:
            break

        for rec in page:
            tag_titles = set()
            for t in (rec.get("tags") or []):
                if isinstance(t, str):
                    tag_titles.add(t.strip().lower())
                elif isinstance(t, dict):
                    title = t.get("title") or t.get("name") or ""
                    if title:
                        tag_titles.add(title.strip().lower())
            # Records tagged tax_delinquent AND NOT already caught_up
            if tag_lc in tag_titles and caught_up_lc not in tag_titles:
                matches.append(rec)

        if len(page) < limit:
            break
        offset += limit
    return matches


def _record_parcel_id(record: dict) -> str:
    """Extract parcel_id from a DataSift record."""
    # Try direct field first
    p = (record.get("parcel_id") or "").strip()
    if p:
        return p.upper()
    # Custom-fields fallback (DataSift sometimes stores parcel there)
    cf = record.get("custom_fields") or {}
    if isinstance(cf, dict):
        p = (cf.get("Parcel ID") or cf.get("parcel_id") or "").strip()
        if p:
            return p.upper()
    return ""


def _mark_caught_up(property_uuid: str, *, dry_run: bool = False) -> bool:
    """Apply `tax_caught_up` tag + remove `tax_delinquent` tag.

    Returns True on success. Uses title-based POST (no reverse UUID lookup).
    """
    if dry_run:
        return True
    try:
        vt.mark_state(property_uuid, vt.RECORD_TAG_TAX_CAUGHT_UP)
    except Exception as e:
        logger.warning("Failed to apply tax_caught_up on %s: %s",
                       property_uuid[:8], e)
        return False
    # Best-effort remove of tax_delinquent
    try:
        ds._post(
            f"/property/{property_uuid}/remove-tags/",
            {"tags": [vt.RECORD_TAG_TAX_DELINQUENT]},
        )
    except Exception as e:
        logger.debug("remove tax_delinquent failed for %s: %s",
                     property_uuid[:8], e)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--current-parcels-file", type=Path,
        help="JSON file with a 'parcels' list of currently-delinquent parcel_ids. "
             "If omitted, reads from this week's tax_distress CSVs in output/leads/",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Log matches without applying tag changes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    # Load current delinquent parcel_ids
    if args.current_parcels_file:
        try:
            payload = json.loads(args.current_parcels_file.read_text())
            current_parcels = {p.strip().upper() for p in (payload.get("parcels") or [])}
        except Exception as e:
            logger.error("Failed to read %s: %s", args.current_parcels_file, e)
            return 1
    else:
        current_parcels = _load_current_parcels_from_csvs()
    logger.info("Current delinquent parcels loaded: %d", len(current_parcels))

    if not current_parcels:
        logger.warning(
            "No current-delinquent parcels found. Refusing to run "
            "'caught-up' detection — would erroneously flag every "
            "tax_delinquent record as caught up. Ensure "
            "tax_distress_pipeline ran and produced a CSV before this step."
        )
        return 1

    # Query all DataSift records currently tagged tax_delinquent
    logger.info("Querying DataSift for records tagged `%s`...",
                vt.RECORD_TAG_TAX_DELINQUENT)
    tagged_records = _query_tax_delinquent_records()
    logger.info("Records currently tagged tax_delinquent: %d",
                len(tagged_records))

    # Cross-reference: for each tagged record, check if its parcel is still in current list
    caught_up_count = 0
    still_delinquent = 0
    no_parcel_id = 0
    for rec in tagged_records:
        parcel = _record_parcel_id(rec)
        if not parcel:
            no_parcel_id += 1
            continue
        if parcel in current_parcels:
            still_delinquent += 1
            continue
        # Parcel not in current list → caught up
        uuid = rec.get("uuid", "")
        if not uuid:
            continue
        street = ((rec.get("address") or {}).get("street") or "?")[:40]
        logger.info(
            "  ✅ %s @ %s (parcel %s) → caught up",
            uuid[:8], street, parcel,
        )
        if _mark_caught_up(uuid, dry_run=args.dry_run):
            caught_up_count += 1

    logger.info(
        "SUMMARY: %d newly caught up · %d still delinquent · %d skipped (no parcel_id)",
        caught_up_count, still_delinquent, no_parcel_id,
    )
    if args.dry_run:
        logger.info("[DRY RUN] no tag changes applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
