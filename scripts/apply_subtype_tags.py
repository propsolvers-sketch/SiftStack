"""Promote a curated set of filter-relevant tags onto freshly-uploaded records.

Why this exists (root cause confirmed 2026-09-05):
  datasift_formatter.write_datasift_csv() writes ONLY "Courthouse Data" (plus
  at most one of 4 subtypes) to the CSV Tags column — by design, because the
  DataSift upload wizard's Tags mapping is unreliable for multi-value cells
  and per prior operator feedback county/date/tier tags cluttered the
  property page. The FULL tag list computed by _build_tags() is appended to
  the Notes column under a "=== TAGS ===" marker instead.

  Consequence: every distress/action/ownership tag the codebase computes
  (foreclosure, probate_sale, hearing_upcoming, municipality_hoover,
  code_enforcement_complaint, tier_distress_proxy_absentee, individual_owner,
  tax_high_exposure, has_auction, auction_next_*, early_distress, demolish,
  heir_of_*, ...) exists ONLY as Notes text — invisible to DataSift filter
  presets. Every preset built on those tags returned 0 records.

  Operator decision 2026-09-05: promote a CURATED filter set (not the full
  list) post-upload via the API. County names, YYYY-MM dates, tier levels,
  living/deceased, confidence, signing-chain and per-phone tags stay
  Notes-only to honor the earlier "don't clutter the page" feedback.

Runs AFTER daily_finalize.py's uploads complete. For each row of each
uploaded CSV:

  1. Parse the "=== TAGS ===" block from the row's Notes column (exactly
     what _build_tags() computed at write time) and union with the
     subtype / notice_type columns.
  2. Intersect with CURATED_PROMOTABLE_TAGS (+ any municipality_* tag).
  3. Resolve the record UUID via the address→UUID index
     (ds.find_property_uuid_by_address — exact street|zip5 key, so the
     UUID→row match is inherent; no ?lists= / ?search= params, both of
     which DataSift silently ignores).
  4. POST all applicable tags in ONE add-tags call.

Cost: 1 amortized index build + 1 POST per row with promotable tags.

Backfill note: historical archives can be replayed with --csv against
each archived upload CSV (see scripts/backfill_curated_tags.py, which
does a two-pass -created / -updated index to reach past DataSift's
10,000-row query cap).

CLI:
    python scripts/apply_subtype_tags.py                    # today's CSVs
    python scripts/apply_subtype_tags.py --csv <path>       # one CSV
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


# ── Curated promotable set ───────────────────────────────────────────
# Every tag here is one a filter preset actually keys on. Anything NOT
# here stays in Notes under === TAGS === (county, YYYY-MM, tier_*,
# in_tier, living/deceased, *_confidence, signing_chain_*, has_heirs,
# phone_*, per-date foreclosure_YYYY-MM-DD / auction_YYYY-MM-DD, homestead).
CURATED_PROMOTABLE_TAGS = frozenset({
    # notice_type markers — replace the dead uppercase FORECLOSURE tag
    "foreclosure", "probate", "pre_probate", "code_violation",
    "tax_delinquent", "tax_sale", "eviction", "divorce",
    # subtypes
    "foreclosure_cancelled", "foreclosure_postponed",
    "probate_sale", "probate_final_settlement", "probate_heirs_notice",
    "unsafe_building", "housing_enforcement", "inoperable_vehicle",
    "environmental_enforcement", "zoning_enforcement",
    "code_enforcement_complaint",
    "tier_distress_proxy", "tier_distress_proxy_absentee",
    # action / timing signals
    "hearing_upcoming", "creditor_window_open", "has_auction",
    "auction_next_7_days", "auction_next_14_days", "auction_next_21_days",
    "early_distress", "demolish", "multi_parcel",
    # ownership / financial exposure
    "individual_owner", "entity_owned",
    "tax_high_exposure", "tax_high_exposure_10k",
    # heir-row markers (heir CSV rows land in the same list as DMs)
    "heir_of_foreclosure", "heir_of_probate", "heir_of_pre_probate",
    "heir_of_tax_delinquent", "heir_of_tax_sale", "heir_of_code_violation",
    "heir_of_eviction", "heir_of_divorce",
})
_PROMOTABLE_PREFIXES = ("municipality_",)

# Kept for backward compatibility with callers/readers that import these.
PROMOTABLE_SUBTYPES = frozenset({
    "foreclosure_cancelled", "foreclosure_postponed",
    "probate_sale", "probate_final_settlement",
})
PROMOTABLE_NOTICE_TYPES = {
    "tax_delinquent": "tax_delinquent",
    "tax_sale":       "tax_delinquent",   # tax sale = still delinquent, same lifecycle
}

_TAGS_MARKER = "=== TAGS ==="


def _is_promotable(tag: str) -> bool:
    t = tag.strip().lower()
    return t in CURATED_PROMOTABLE_TAGS or t.startswith(_PROMOTABLE_PREFIXES)


def _parse_notes_tags(notes: str) -> set[str]:
    """Extract the tag list _build_tags() appended under === TAGS === in Notes.

    Format (datasift_formatter.write_datasift_csv): the marker is appended
    LAST, followed by one comma-joined line. Returns lowercased tags.
    """
    if not notes or _TAGS_MARKER not in notes:
        return set()
    block = notes.split(_TAGS_MARKER, 1)[1].strip()
    first_line = block.splitlines()[0] if block else ""
    return {t.strip().lower() for t in first_line.split(",") if t.strip()}


def tags_for_row(row: dict) -> set[str]:
    """All promotable tags for one CSV row (Notes block ∪ subtype ∪ notice_type)."""
    tags = _parse_notes_tags(row.get("Notes") or "")
    subtype = (row.get("Probate Subtype") or "").strip().lower()
    if subtype:
        tags.add(subtype)
    notice_type = (row.get("Notice Type") or "").strip().lower()
    if notice_type:
        tags.add(notice_type)
        mapped = PROMOTABLE_NOTICE_TYPES.get(notice_type)
        if mapped:
            tags.add(mapped)
    return {t for t in tags if _is_promotable(t)}


def _find_property_uuid(street: str, zip5: str, *, ordering: str = "-created") -> str | None:
    """Look up DataSift property UUID by street + zip5 via the paginated index.

    2026-08-26: replaces ?search= (silently ignored by DataSift). The index
    key is exact street|zip5, so a hit is inherently the right record —
    which is why no post-resolve list validation is done here (and why
    the ?lists=-based FORECLOSURE backfill was wrong: that param is also
    silently ignored and returned records by recency).
    """
    if not (street and zip5):
        return None
    try:
        return ds.find_property_uuid_by_address(street, zip5.strip()[:5], ordering=ordering)
    except Exception as e:
        logger.debug("Property index lookup failed for %r: %s", street, e)
        return None


def apply_subtypes_from_csv(csv_path: Path, *, dry_run: bool = False,
                            ordering: str = "-created",
                            unresolved: list[dict] | None = None) -> dict:
    """Promote curated tags for every row of one CSV.

    ``unresolved`` (optional): rows whose address didn't resolve to a UUID
    are appended here so a backfill caller can retry them against a
    second index ordering.
    """
    stats = {
        "csv": csv_path.name,
        "rows_total": 0,
        "rows_with_tags": 0,
        "rows_with_subtype": 0,      # compat alias for daily_finalize
        "rows_matched_uuid": 0,
        "rows_tagged": 0,
        "tags_applied": 0,
        "rows_failed": 0,
    }
    if not csv_path.exists():
        logger.warning("CSV not found: %s", csv_path)
        return stats

    with csv_path.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stats["rows_total"] += 1
            tags = tags_for_row(row)
            if not tags:
                continue
            stats["rows_with_tags"] += 1
            stats["rows_with_subtype"] += 1

            street = (row.get("Property Street Address") or "").strip()
            zip5 = (row.get("Property ZIP Code") or "").strip()[:5]
            uuid = _find_property_uuid(street, zip5, ordering=ordering)
            if not uuid:
                stats["rows_failed"] += 1
                if unresolved is not None:
                    unresolved.append(row)
                logger.warning("  ❌ no DataSift match for %s %s (tags=%s)",
                               street, zip5, sorted(tags))
                continue
            stats["rows_matched_uuid"] += 1

            sorted_tags = sorted(tags)
            if dry_run:
                logger.info("  [DRY] would tag %s (%s) with %s",
                            uuid[:8], street[:40], sorted_tags)
                continue

            try:
                for t in sorted_tags:
                    ds.tag_uuid(t, create_if_missing=True)   # idempotent ensure-exists
                ds._post(f"/property/{uuid}/add-tags/", {"tags": sorted_tags})
                stats["rows_tagged"] += 1
                stats["tags_applied"] += len(sorted_tags)
                logger.info("  ✅ tagged %s (%s) with %s",
                            uuid[:8], street[:40], sorted_tags)
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

    logger.info("Processing %d CSV(s) for curated tag promotion", len(csvs))
    total_tagged = total_tags = total_failed = 0
    for csv_path in csvs:
        stats = apply_subtypes_from_csv(csv_path, dry_run=args.dry_run)
        if stats["rows_with_tags"]:
            logger.info(
                "  %s → %d/%d rows tagged (%d tags applied, %d matched UUID, %d failed)",
                csv_path.name, stats["rows_tagged"], stats["rows_with_tags"],
                stats["tags_applied"], stats["rows_matched_uuid"], stats["rows_failed"],
            )
        total_tagged += stats["rows_tagged"]
        total_tags += stats["tags_applied"]
        total_failed += stats["rows_failed"]

    logger.info("Curated tag promotion complete: %d records, %d tags applied, %d failed",
                total_tagged, total_tags, total_failed)
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
