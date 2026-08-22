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

# DataSift's /property/{uuid} response returns `lists` as a list of TITLE
# strings, not UUIDs (diagnostic 2026-08-21). So client-side list membership
# checks must compare against titles, not UUIDs.
OBITUARY_UNIVERSE_LIST_TITLES = frozenset({
    "Obituary",
    "Probate",
    "Pre-Probate/Deceased",
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
    target_zips: frozenset[str] | None = None,
    limit: int | None = 500,
    page_size: int = 500,
    max_candidates: int = 100,
) -> list[dict]:
    """Query DataSift for records in the probate/obituary universe.

    Rewritten 2026-08-21 (v2) to fix the actual root cause: DataSift's
    /property/ list endpoint returns records with `list_count` + `tag_count`
    (integers) but NO `lists` or `tags` fields with the actual UUIDs.
    Client-side list/tag validation was silently returning False on every
    record because those fields don't exist in the response.

    Correct pattern (works around the API's list-endpoint limitation):

      1. Query with ?lists=<probate universe> + ordering=-created
         → returns candidate UUIDs (server-side list filter — trusted)
      2. Filter candidates by property ZIP (address IS in the list response)
      3. For each ZIP-passing candidate, fetch FULL record via
         get_property(uuid) → returns {tags, lists, ...} with UUIDs
      4. Client-side validate list membership (belt+suspenders)
      5. Filter out records already tagged traced_smartskip
      6. Return up to `limit` matching records

    Trade-off: this makes N GET calls (1 per candidate). For probate universe
    daily volume (~30 fresh records + backlog), that's typically 50-200 extra
    GETs = a few seconds. Small cost for reliability.

    Args:
      require_traced_smartskip_missing: filter out records already SmartSkip'd
      target_zips: keep ONLY records whose property ZIP is in this set (recommended
        — cheap ZIP filter runs BEFORE expensive per-record fetches)
      limit: max matching records to return
      page_size: records per API list call (default 500)
      max_candidates: hard cap on how many candidates we fetch full-detail for
        (safety net — prevents runaway GET calls if server filter is broken)
    """
    from vendor_tags import RECORD_TAG_TRACED_SMARTSKIP  # avoid circular

    smartskip_uuid = None
    if require_traced_smartskip_missing:
        smartskip_uuid = ds.tag_uuid(RECORD_TAG_TRACED_SMARTSKIP, create_if_missing=False)

    def _in_target_zip(r: dict) -> bool:
        """Cheap ZIP filter using the address field (present in list response)."""
        if not target_zips:
            return True
        addr = r.get("address") or {}
        zip5 = (addr.get("zip5") or addr.get("postal_code") or "").strip()[:5]
        return zip5 in target_zips

    # DataSift returns list/tag membership as TITLES (strings), not UUIDs.
    # Discovered via diagnostic 2026-08-21 — full['lists'] = ['Probate', ...].
    from vendor_tags import RECORD_TAG_TRACED_SMARTSKIP as _SMARTSKIP_TITLE

    _SMARTSKIP_TITLE_LC = _SMARTSKIP_TITLE.strip().lower()

    def _has_smartskip_full(full_rec: dict) -> bool:
        """Tag check by TITLE (case-insensitive) — full['tags'] holds title strings."""
        if not require_traced_smartskip_missing:
            return False
        for t in (full_rec.get("tags") or []):
            title = t if isinstance(t, str) else (
                (t.get("title") or t.get("name") or "") if isinstance(t, dict) else ""
            )
            if title and title.strip().lower() == _SMARTSKIP_TITLE_LC:
                return True
        return False

    # Case-insensitive lookup set to handle any capitalization/whitespace
    # variance in DataSift's returned list titles (defensive after 2026-08-22
    # miss where fresh records weren't in list at query time due to routing lag).
    _NORMALIZED_UNIVERSE = frozenset(t.strip().lower() for t in OBITUARY_UNIVERSE_LIST_TITLES)
    _PROBATE_NOTICE_TYPES_LC = frozenset({"probate", "pre_probate", "obituary"})

    def _in_probate_universe_full(full_rec: dict) -> bool:
        """Membership check with 2 signals — tolerant to DataSift routing lag:

        1. Case-insensitive list-title match (Probate, Pre-Probate/Deceased, Obituary)
        2. notice_type fallback (probate/pre_probate/obituary)

        Either signal counts as membership. Notice_type is set at upload time
        (no routing lag), so it catches records that were just uploaded but
        haven't been added to the list yet by DataSift's background job.
        """
        # Signal 1: list title (may lag ~5-15 min after upload)
        for lst in (full_rec.get("lists") or []):
            title = lst if isinstance(lst, str) else (
                (lst.get("title") or lst.get("name") or "") if isinstance(lst, dict) else ""
            )
            if title and title.strip().lower() in _NORMALIZED_UNIVERSE:
                return True
        # Signal 2: notice_type field (set at upload — no lag)
        notice_type = (full_rec.get("notice_type") or "").strip().lower()
        if notice_type in _PROBATE_NOTICE_TYPES_LC:
            return True
        return False

    HARD_CAP = 9500
    scan_cap = min(max_candidates * 5, HARD_CAP)  # scan up to 5x candidate cap
    return_cap = limit if limit is not None else max_candidates

    # ── Stage 1: fetch candidate UUIDs via list query (ordering=-created) ──
    candidates: list[dict] = []
    offset = 0
    while len(candidates) < scan_cap:
        params = {
            "lists": ",".join(OBITUARY_UNIVERSE_LIST_UUIDS),
            "limit": page_size,
            "offset": offset,
            "ordering": "-created",
        }
        resp = ds._get("/property/", params)
        data = resp.get("data") or resp.get("results") or []
        if not data:
            break
        candidates.extend(data)
        if len(data) < page_size:
            break
        offset += page_size

    # Cheap ZIP filter FIRST (address is in list response)
    zip_filtered = [c for c in candidates if _in_target_zip(c)]
    logger.info(
        "Stage 1 candidates: %d fetched → %d after ZIP filter (%s ZIPs)",
        len(candidates), len(zip_filtered),
        f"{len(target_zips)} target" if target_zips else "any",
    )

    # ── Stage 2: fetch FULL details for ZIP-passing candidates ──
    # Cap the number of expensive GET calls
    to_fetch = zip_filtered[:max_candidates]
    if len(zip_filtered) > max_candidates:
        logger.warning(
            "Truncating candidate fetch: %d → %d (max_candidates cap). Increase "
            "cap if truncation is dropping legitimate matches.",
            len(zip_filtered), max_candidates,
        )

    matching: list[dict] = []
    probate_confirmed = 0
    already_smartskip = 0
    fetch_errors = 0
    for i, cand in enumerate(to_fetch):
        if len(matching) >= return_cap:
            break
        uuid = cand.get("uuid")
        if not uuid:
            continue
        try:
            full = ds.get_property(uuid)
        except Exception as e:
            fetch_errors += 1
            logger.debug("get_property(%s) failed: %s", uuid[:8], e)
            continue
        if not _in_probate_universe_full(full):
            continue  # server ?lists= filter returned a non-probate record
        probate_confirmed += 1
        if _has_smartskip_full(full):
            already_smartskip += 1
            continue
        matching.append(full)

    logger.info(
        "Stage 2 full-detail check: fetched %d → %d confirmed probate → "
        "%d already SmartSkip'd → %d matching (fetch_errors=%d)",
        len(to_fetch), probate_confirmed, already_smartskip, len(matching),
        fetch_errors,
    )
    logger.info(
        "SmartSkip queue: %d records ready to submit "
        "(pipeline: %d candidates → %d in scope → %d confirmed probate → "
        "%d pending SmartSkip)",
        len(matching), len(candidates), len(zip_filtered),
        probate_confirmed, len(matching),
    )
    return matching


__all__ = [
    "Cascade", "RoutedRecord",
    "OBITUARY_UNIVERSE_LIST_UUIDS", "PROBATE_NOTICE_TYPES",
    "route_record", "route_records",
    "query_probate_universe_records",
]
