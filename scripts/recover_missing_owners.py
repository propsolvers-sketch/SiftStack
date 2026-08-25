"""Recover missing owner names on today's probate records via Enformion AddressID.

Bucket D from the pricing diagnostic — records in probate universe with property
address but no First/Last Name on the owner. These records currently CAN'T
go through SmartSkip (SmartSkip requires owner name) → they miss heir enrichment.

This script:
  1. Reads today_probate_uuids.json (written by capture_today_probate_uuids.py)
  2. For each UUID, fetches the record + checks if owner name is missing
  3. If missing: calls Enformion AddressID on the mailing address (or property
     address as fallback) to recover the owner's likely name
  4. PATCHes the DataSift owner record with the recovered name
  5. Tags the record `owner_backfilled_enformion` for audit/filter

Cost: ~$0.10/lookup. Only runs on records that would otherwise be lost.

Runs BEFORE SmartSkip step in the workflow so recovered records get
SmartSkip'd same-day (their Path A UUID is still in the JSON, and now
they have owner names → SmartSkip will accept them).

CLI:
    python scripts/recover_missing_owners.py                # today's captured UUIDs
    python scripts/recover_missing_owners.py --uuid <uuid>  # single record
    python scripts/recover_missing_owners.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
import enformion_client as enf

logger = logging.getLogger(__name__)

CAPTURED_JSON = Path(__file__).parent.parent / "output" / "observability" / "today_probate_uuids.json"
BACKFILL_TAG = "owner_backfilled_enformion"


def _owner_name_missing(record: dict) -> bool:
    """True if first_name OR last_name is empty/whitespace on the owner."""
    owner = record.get("owner") or {}
    if not isinstance(owner, dict):
        return True
    first = (owner.get("first_name") or "").strip()
    last = (owner.get("last_name") or "").strip()
    return not (first and last)


def _mailing_address(record: dict) -> tuple[str, str, str, str] | None:
    """Prefer OWNER mailing address (where they receive mail — likely them),
    falling back to PROPERTY address (could be tenant on rental).

    Returns (street, city, state, zip5) or None if incomplete.
    """
    owner = record.get("owner") or {}
    mailing = (owner.get("address") or {}) if isinstance(owner, dict) else {}
    prop = record.get("address") or {}

    for candidate in (mailing, prop):
        street = (candidate.get("street") or "").strip()
        city = (candidate.get("city") or "").strip()
        state = (candidate.get("state") or "").strip()
        zip5 = (candidate.get("zip5") or candidate.get("postal_code") or "").strip()[:5]
        if street and state and zip5:
            return street, city, state, zip5
    return None


def _apply_backfill_tag(property_uuid: str) -> None:
    """Tag the record so operator can filter DataSift for backfilled records."""
    try:
        ds.tag_uuid(BACKFILL_TAG, create_if_missing=True)
        ds._post(f"/property/{property_uuid}/add-tags/", {"tags": [BACKFILL_TAG]})
    except Exception as e:
        logger.debug("apply_backfill_tag failed for %s: %s", property_uuid[:8], e)


def recover_one(property_uuid: str, *, dry_run: bool = False) -> dict:
    """Recover owner name for a single record. Returns stats dict."""
    stats = {
        "uuid": property_uuid,
        "action": "skipped",
        "reason": "",
        "recovered_first": "",
        "recovered_last": "",
    }
    try:
        record = ds.get_property(property_uuid)
    except Exception as e:
        stats["reason"] = f"get_property failed: {e}"
        return stats

    if not _owner_name_missing(record):
        stats["reason"] = "owner name already present"
        return stats

    addr = _mailing_address(record)
    if not addr:
        stats["reason"] = "no usable mailing/property address"
        return stats
    street, city, state, zip5 = addr

    if not enf.is_configured():
        stats["reason"] = "Enformion not configured"
        return stats

    resp = enf.address_id(street=street, city=city, state=state, zip_code=zip5)
    person = enf.address_id_person(resp)
    if not person:
        stats["action"] = "no_match"
        stats["reason"] = "AddressID returned no person"
        return stats

    first = (enf._get_first_name(person) or "").strip()
    last = (enf._get_last_name(person) or "").strip()
    if not (first and last):
        stats["action"] = "partial_match"
        stats["reason"] = "AddressID person missing first or last name"
        return stats
    stats["recovered_first"] = first
    stats["recovered_last"] = last

    if dry_run:
        stats["action"] = "would_recover"
        logger.info("  [DRY] would update %s owner → %s %s (from AddressID)",
                    property_uuid[:8], first, last)
        return stats

    owner = record.get("owner") or {}
    owner_uuid = owner.get("uuid")
    if not owner_uuid:
        stats["reason"] = "record has no owner_uuid — cannot PATCH"
        return stats

    try:
        ds.update_owner_name(owner_uuid, first_name=first, last_name=last)
        _apply_backfill_tag(property_uuid)
        stats["action"] = "recovered"
        logger.info("  ✅ %s owner updated → %s %s + tagged %s",
                    property_uuid[:8], first, last, BACKFILL_TAG)
    except Exception as e:
        stats["reason"] = f"PATCH failed: {e}"
        logger.warning("  ❌ %s owner update failed: %s", property_uuid[:8], e)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uuid", help="Recover a single record by UUID")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be recovered without writing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    if args.uuid:
        uuids = [args.uuid]
    elif CAPTURED_JSON.exists():
        try:
            payload = json.loads(CAPTURED_JSON.read_text())
            uuids = payload.get("uuids") or []
        except Exception as e:
            logger.error("Failed to load %s: %s", CAPTURED_JSON, e)
            return 1
    else:
        logger.info("No captured UUIDs file — run capture_today_probate_uuids.py first")
        return 0

    if not uuids:
        logger.info("No UUIDs to check — nothing to do")
        return 0

    logger.info("Checking %d record(s) for missing owner info...", len(uuids))
    stats_list = [recover_one(u, dry_run=args.dry_run) for u in uuids]

    # Summarize
    from collections import Counter
    actions = Counter(s["action"] for s in stats_list)
    logger.info("Summary: %s", dict(actions))
    recovered = [s for s in stats_list if s["action"] == "recovered"]
    if recovered:
        logger.info("Recovered names:")
        for s in recovered[:10]:
            logger.info("  · %s → %s %s",
                        s["uuid"][:8], s["recovered_first"], s["recovered_last"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
