"""Enrichment router — dispatches each record to the correct skip-trace cascade.

Two lanes:

  4-vendor probate-universe cascade  (SmartSkip first, then Tracerfy →
                                       DataSift → Enformion → Trestle)
  3-vendor standard cascade          (Tracerfy → DataSift → Enformion → Trestle)

Probate-universe membership = ANY of:
  notice_type ∈ {"probate", "pre_probate"}
  OR record appears in one of the "obituary/deceased" lists:
     - Obituary
     - Probate
     - Pre-Probate/Deceased

That last set is what Filter Preset "11.02 Obituary / Deceased" filters on.
Records added to those lists AFTER their initial daily-sweep enrichment get
picked up by the WEEKLY REHASH pass (queries this filter for records lacking
`traced_smartskip`).

The router is intentionally thin — just routing logic + the API queries that
build the record-to-cascade assignment. Actual cascade implementations live
in scripts/probate_cascade.py and scripts/standard_cascade.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import datasift_api as ds

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# List UUIDs that define the "obituary/deceased" universe
# ─────────────────────────────────────────────────────────────────────

# Union of these = records that should go through the 4-vendor probate cascade
# regardless of their notice_type. Mirrors the filter on Preset "11.02".
OBITUARY_UNIVERSE_LIST_UUIDS = frozenset({
    "b26779e6-8c21-485c-8033-594209d8e9e2",  # Obituary
    "6094859c-3d7b-47b3-ab06-01b606e54e15",  # Probate
    "9349c0c5-d46e-4621-8ebf-0f0de2c93fcf",  # Pre-Probate/Deceased
})

PROBATE_NOTICE_TYPES = frozenset({"probate", "pre_probate"})


# ─────────────────────────────────────────────────────────────────────
# Cascade lane
# ─────────────────────────────────────────────────────────────────────


class Cascade(str, Enum):
    """Which cascade a record should flow through."""
    PROBATE_4VENDOR  = "probate_4vendor"       # SmartSkip → Tracerfy → DataSift → Enformion → Trestle
    STANDARD_3VENDOR = "standard_3vendor"      # Tracerfy → DataSift → Enformion → Trestle


@dataclass
class RoutedRecord:
    """Result of routing one record."""
    property_uuid: str
    notice_type: str
    list_uuids: list[str]
    cascade: Cascade
    reason: str        # human-readable why this cascade was chosen


# ─────────────────────────────────────────────────────────────────────
# Routing logic
# ─────────────────────────────────────────────────────────────────────


def _list_uuids_on(record: dict) -> list[str]:
    """Extract list UUIDs from a DataSift record's `lists` field.

    DataSift's /property/ response nests lists as [{uuid, title, ...}, ...].
    """
    lists = record.get("lists") or []
    if not lists:
        return []
    if isinstance(lists[0], dict):
        return [l.get("uuid", "") for l in lists if l.get("uuid")]
    # Some endpoints may return bare UUID strings — fall through
    return [str(l) for l in lists if l]


def _notice_type_of(record: dict) -> str:
    """Best-effort extraction of notice_type.

    DataSift doesn't have a single canonical notice_type field on the
    property record — it's derived from tags (e.g. tag `probate`, `foreclosure`)
    or from list membership. We accept either shape:

      record["notice_type"]         — if the caller has already resolved it
      record["tags"]                — list of {uuid, title} — look for known types
    """
    nt = (record.get("notice_type") or "").strip().lower()
    if nt:
        return nt
    for tag in (record.get("tags") or []):
        title = (tag.get("title") if isinstance(tag, dict) else str(tag)) or ""
        for candidate in PROBATE_NOTICE_TYPES:
            if title.strip().lower() == candidate:
                return candidate
    return ""


def route_record(record: dict) -> RoutedRecord:
    """Decide which cascade a single record should flow through.

    Priority order:
      1. notice_type ∈ {probate, pre_probate} → PROBATE_4VENDOR
      2. any list in obituary universe → PROBATE_4VENDOR
      3. default → STANDARD_3VENDOR
    """
    property_uuid = record.get("uuid", "")
    nt = _notice_type_of(record)
    list_uuids = _list_uuids_on(record)

    if nt in PROBATE_NOTICE_TYPES:
        return RoutedRecord(
            property_uuid=property_uuid,
            notice_type=nt,
            list_uuids=list_uuids,
            cascade=Cascade.PROBATE_4VENDOR,
            reason=f"notice_type={nt}",
        )

    intersect = set(list_uuids) & OBITUARY_UNIVERSE_LIST_UUIDS
    if intersect:
        return RoutedRecord(
            property_uuid=property_uuid,
            notice_type=nt,
            list_uuids=list_uuids,
            cascade=Cascade.PROBATE_4VENDOR,
            reason=f"in_obituary_universe (lists: {sorted(intersect)})",
        )

    return RoutedRecord(
        property_uuid=property_uuid,
        notice_type=nt,
        list_uuids=list_uuids,
        cascade=Cascade.STANDARD_3VENDOR,
        reason="default (not probate + not in obituary universe)",
    )


def route_records(records: Iterable[dict]) -> dict[Cascade, list[RoutedRecord]]:
    """Bucket a batch of records into their target cascades."""
    buckets: dict[Cascade, list[RoutedRecord]] = {
        Cascade.PROBATE_4VENDOR: [],
        Cascade.STANDARD_3VENDOR: [],
    }
    for rec in records:
        routed = route_record(rec)
        buckets[routed.cascade].append(routed)
    return buckets


# ─────────────────────────────────────────────────────────────────────
# Query helpers — pull records that need cascading
# ─────────────────────────────────────────────────────────────────────


def query_probate_universe_records(
    *,
    require_traced_smartskip_missing: bool = False,
    limit: int | None = 500,
    page_size: int = 500,
) -> list[dict]:
    """Query DataSift for records in the probate/obituary universe.

    Args:
      require_traced_smartskip_missing: if True, filter out records already
        tagged `traced_smartskip` client-side (used by WEEKLY REHASH pass).
      limit: max total records to return (None = up to DataSift's 10K cap).
      page_size: per-page fetch size.

    DataSift's /property/ has a hard 10K cap on offset+limit — we respect it.
    """
    from vendor_tags import RECORD_TAG_TRACED_SMARTSKIP  # avoid circular

    smartskip_uuid = None
    if require_traced_smartskip_missing:
        smartskip_uuid = ds.tag_uuid(RECORD_TAG_TRACED_SMARTSKIP, create_if_missing=False)

    # DataSift's 10K offset+limit cap
    HARD_CAP = 9500
    effective_max = min(limit or HARD_CAP, HARD_CAP)

    all_records: list[dict] = []
    offset = 0
    while len(all_records) < effective_max:
        page = min(page_size, effective_max - len(all_records))
        params = {
            "lists": ",".join(OBITUARY_UNIVERSE_LIST_UUIDS),
            "limit": page,
            "offset": offset,
        }
        resp = ds._get("/property/", params)
        data = resp.get("data") or resp.get("results") or []
        if not data:
            break
        all_records.extend(data)
        if len(data) < page:
            break
        offset += page

    # Client-side filter for the traced_smartskip exclusion
    if smartskip_uuid:
        def _has_smartskip(r):
            tags = r.get("tags") or []
            for t in tags:
                uid = t.get("uuid") if isinstance(t, dict) else t
                if uid == smartskip_uuid:
                    return True
            return False
        before = len(all_records)
        all_records = [r for r in all_records if not _has_smartskip(r)]
        logger.info("Filtered smartskip-already-traced: %d → %d", before, len(all_records))

    return all_records


__all__ = [
    "Cascade", "RoutedRecord",
    "OBITUARY_UNIVERSE_LIST_UUIDS", "PROBATE_NOTICE_TYPES",
    "route_record", "route_records",
    "query_probate_universe_records",
]
