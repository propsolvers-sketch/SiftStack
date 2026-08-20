"""Three-vendor skip-trace cascade for freshly-uploaded DataSift records.

The DataSift Open API dropped 2026-07-25, unblocking the last piece of a
long-standing pipeline gap: after Tracerfy runs during the daily cron
and records land in DataSift, we can NOW also hit DataSift's own native
skip-trace + per-phone-tag the results via ``POST /property/{uuid}/add-phone-tag/``.

Cascade per record (skip-trace vendors are additive — each may find phones
the others missed):

  1. Tracerfy  — runs UPSTREAM in the pipeline; phones already on the record
  2. DataSift  — this script triggers ``POST /property/skip-trace/`` and
                 polls Activity until DataSift's built-in vendor completes
  3. Enformion — runs UPSTREAM in pre_probate_pipeline_al when quota allows
                 (currently 429-blocked until 2026-08-01 monthly reset;
                 auto-resumes then)

After all 3 vendors have contributed, every unique phone on the record
gets Trestle-scored, mapped to a Dial-First/Second/Third/Fourth/Drop
tier, and tagged via ``add_phone_tag`` so DataSift filter presets on
"phone_tags:Dial First" work end-to-end.

Runs as a new step in ``.github/workflows/daily-sweep.yml`` after all
uploads complete. Idempotent — re-running skips records that already
have Trestle tags on every phone.

Usage:
  # Overnight cron (default: all new records from last 24h)
  python scripts/thorough_skip_trace.py

  # Manual: a specific list, custom lookback
  python scripts/thorough_skip_trace.py --list Foreclosure --since-hours 48

  # Dry-run (logs decisions, no vendor spend, no writes)
  python scripts/thorough_skip_trace.py --dry-run

  # Just one record (for testing)
  python scripts/thorough_skip_trace.py --property-uuid <uuid>
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
import phone_validator as pv
import config as cfg
import enformion_client as enf
import vendor_tags as vt

logger = logging.getLogger(__name__)


def _record_has_tag(record: dict, tag_name: str) -> bool:
    """True if `record` has a tag with the given title OR the tag's UUID.

    DataSift's property GET may return tags as {uuid, title} or just uuid
    strings — we check both. Case-insensitive on title match.
    """
    tags = record.get("tags") or []
    lowered = tag_name.strip().lower()
    for t in tags:
        if isinstance(t, dict):
            title = (t.get("title") or "").strip().lower()
            if title == lowered:
                return True
        elif isinstance(t, str):
            if t.strip().lower() == lowered:
                return True
    return False


def _build_notice_data_from_property(prop: dict):
    """Build a minimal NoticeData object from a DataSift property record.

    Used to hand off to tracerfy_skip_tracer.batch_skip_trace which expects
    NoticeData objects. Returns None if the record lacks required fields
    (owner name + property address).
    """
    try:
        from notice_data import NoticeData
    except Exception:
        return None

    owner = prop.get("owner") or {}
    first = (owner.get("first_name") or "").strip()
    last = (owner.get("last_name") or "").strip()
    if not (first and last):
        return None

    prop_addr = prop.get("address") or {}
    street = (prop_addr.get("street") or "").strip()
    if not street:
        return None

    owner_addr = owner.get("address") or {}
    return NoticeData(
        # Property (used by Tracerfy for address matching)
        address=street,
        city=(prop_addr.get("city") or "").strip(),
        state=(prop_addr.get("state") or "").strip(),
        zip=(prop_addr.get("zip5") or prop_addr.get("postal_code") or "")[:5],
        # Owner name
        owner_name=f"{first} {last}",
        owner_first_name=first,
        owner_last_name=last,
        # Mailing (Tracerfy uses this if property != mailing)
        owner_street=(owner_addr.get("street") or "").strip(),
        owner_city=(owner_addr.get("city") or "").strip(),
        owner_state=(owner_addr.get("state") or "").strip(),
        owner_zip=(owner_addr.get("zip5") or owner_addr.get("postal_code") or "")[:5],
        # Meta (Tracerfy needs a notice_type for tagging heirs; probate is safe default)
        notice_type="probate",
        county=(prop_addr.get("county") or "Jefferson").strip() or "Jefferson",
        source_url="",
        date_added=datetime.now().strftime("%Y-%m-%d"),
    )


def _record_has_smartskip_note_lock(record: dict) -> bool:
    """True if SmartSkip wrote a definitive family-tree Note on this record.

    Signal = has `traced_smartskip` tag AND does NOT have `smartskip_no_match` tag.
    When True, this cascade must NOT append its own summary Note — SmartSkip owns it.
    """
    tags = record.get("tags") or []
    tag_titles = {(t.get("title") or "").strip().lower() if isinstance(t, dict) else str(t).lower()
                  for t in tags}
    return (vt.RECORD_TAG_TRACED_SMARTSKIP.lower() in tag_titles
            and vt.RECORD_TAG_SMARTSKIP_NO_MATCH.lower() not in tag_titles)


def _tag_new_phones_with_source(
    property_uuid: str,
    new_phones: set[str],
    vendor: str,
) -> None:
    """Apply `src:<vendor>` tag to each new phone this vendor sourced."""
    if not new_phones:
        return
    try:
        src_tag_uuid = vt.src_phone_tag_uuid(vendor)
    except Exception as e:
        logger.debug("src_phone_tag lookup failed for %s: %s", vendor, e)
        return
    for phone in new_phones:
        try:
            ds.add_phone_tag(property_uuid, phone, [src_tag_uuid])
        except Exception as e:
            logger.debug("apply src:%s to %s failed: %s", vendor, phone, e)


# ── DataSift polling knobs ──────────────────────────────────────────

# DataSift's skip-trace is async. Per-record turnaround is usually 30-90s
# but a large batch can queue for several minutes. Poll every 20s; give
# up after 15 min so we never block the whole cron on one stuck batch.
_POLL_INTERVAL_S = 20
_POLL_MAX_S = 15 * 60

# DataSift's skip-trace endpoint accepts up to N properties per call.
# Not documented; 50 is a safe conservative batch size.
_SKIP_TRACE_BATCH_SIZE = 50


@dataclass
class RecordResult:
    """Per-record outcome for the daily summary."""
    property_uuid: str
    address: str = ""
    action: str = "processed"       # 'processed' / 'noop' / 'skipped' / 'error'
    phones_before: int = 0
    phones_after: int = 0
    new_phones_datasift: int = 0    # phones DataSift native skip-trace added
    new_phones_enformion: int = 0   # phones Enformion household_search added
    enformion_ran: bool = False     # whether we called Enformion (had owner + address)
    phones_tiered: int = 0
    tier_distribution: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def new_phones(self) -> int:
        """Total new phones added by all vendors this run."""
        return self.new_phones_datasift + self.new_phones_enformion


def _norm_phone(p: str) -> str:
    """10-digit E.164-lite form. Handles country code + separators."""
    d = "".join(c for c in (p or "") if c.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d[-10:] if len(d) >= 10 else ""


def _existing_phone_set(prop: dict) -> set[str]:
    """Every unique 10-digit phone on this property (owner + secondary)."""
    phones: set[str] = set()
    owner = prop.get("owner") or {}
    for ph in (owner.get("phones") or []):
        n = _norm_phone(ph.get("number") or "")
        if n:
            phones.add(n)
    # Some records nest secondary owners; walk them too
    for sec_owner in (prop.get("secondary_owners") or []):
        for ph in (sec_owner.get("phones") or []):
            n = _norm_phone(ph.get("number") or "")
            if n:
                phones.add(n)
    return phones


def _fetch_records(
    *, list_name: str | None, since_dt: datetime, max_records: int = 500,
    tag_filter: str | None = None,
) -> list[dict]:
    """Pull records from DataSift that need cascade processing.

    Selection logic (client-side because DataSift's /property/ API doesn't
    reliably filter by tag or timestamp per prior diagnostics):

      1. Fetch pages of records (up to page_limit * safety_buffer)
      2. If tag_filter is set: keep ONLY records carrying that tag
         (e.g. --tag queue_cascade selects operator-queued records)
      3. Otherwise: keep records LACKING `traced_tracerfy` (never cascaded)
         — this replaces the old `created`-timestamp filter which was
         silently no-op'ing because DataSift's /property/ response doesn't
         include a `created` field
      4. If list_name is set: further filter to records in that list
      5. Return first max_records survivors

    `since_dt` retained for interface compatibility but unused — the
    tag-absence filter naturally excludes previously-processed records
    without needing a time cutoff.
    """
    from vendor_tags import RECORD_TAG_TRACED_TRACERFY, RECORD_TAG_QUEUE_CASCADE

    list_filter_uuid: str | None = None
    if list_name:
        list_filter_uuid = ds.list_uuid(list_name, create_if_missing=False)
        if not list_filter_uuid:
            logger.warning("List %r not found in DataSift — nothing to process", list_name)
            return []

    # Resolve tag UUIDs once (cached in datasift_api)
    tracerfy_tag_uuid = ds.tag_uuid(RECORD_TAG_TRACED_TRACERFY, create_if_missing=False)
    queue_tag_uuid = ds.tag_uuid(RECORD_TAG_QUEUE_CASCADE, create_if_missing=False) if tag_filter == RECORD_TAG_QUEUE_CASCADE else None

    def _record_has_tag(rec: dict, uid: str | None) -> bool:
        if not uid:
            return False
        for t in (rec.get("tags") or []):
            tuid = t.get("uuid") if isinstance(t, dict) else t
            if tuid == uid:
                return True
        return False

    results: list[dict] = []
    limit = 100
    offset = 0
    # Safety buffer — page up to 10x max_records looking for survivors
    max_offset = max_records * 10

    while len(results) < max_records and offset < max_offset:
        params = {"limit": limit, "offset": offset, "ordering": "-created"}
        resp = ds._get("/property/", params)
        page = resp.get("data") or resp.get("results") or []
        if not page:
            break

        for row in page:
            # Apply tag filter — either "must have queue_cascade" or
            # "must lack traced_tracerfy" (default: never-cascaded)
            if tag_filter == RECORD_TAG_QUEUE_CASCADE:
                if not _record_has_tag(row, queue_tag_uuid):
                    continue
            else:
                if _record_has_tag(row, tracerfy_tag_uuid):
                    continue  # skip already-cascaded records
            results.append(row)
            if len(results) >= max_records:
                break

        if len(page) < limit:
            break
        offset += limit

    logger.info(
        "Fetched %d records after tag filter (tag_filter=%r, pages walked=%d)",
        len(results), tag_filter or "not-traced-yet", offset // limit,
    )
    return _apply_list_filter(results, list_filter_uuid)


def _apply_list_filter(records: list[dict], list_uuid: str | None) -> list[dict]:
    """Local filter: keep records whose lists include the target UUID."""
    if not list_uuid:
        return records
    return [
        r for r in records
        if list_uuid in (r.get("lists") or [])
    ]


def _trigger_datasift_skip_trace(property_uuids: list[str]) -> str | None:
    """Fire ``POST /property/skip-trace/`` for a batch of UUIDs.

    Returns the Activity UUID if the response includes one; None otherwise
    (some builds return the property list directly without an activity id).
    """
    resp = ds._post(
        "/property/skip-trace/",
        {"properties": property_uuids},
    )
    # Response shape varies; probe common keys
    if isinstance(resp, dict):
        return resp.get("activity_uuid") or resp.get("uuid") or resp.get("activity_id")
    return None


def _wait_for_activity_completion(
    activity_uuid: str | None, expected_min_records: int,
    *, timeout_s: int = _POLL_MAX_S,
) -> bool:
    """Poll ``GET /activity/?type=skip_trace`` until the batch completes.

    If we have an activity_uuid, look for it by ID and check its status.
    Otherwise fall back to polling recent skip_trace activities and
    assuming the most recent one is ours (with a matching-total heuristic).
    Returns True on completion, False on timeout.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        try:
            acts = ds.list_activity("skip_trace", limit=10)
        except Exception as e:
            logger.warning("Activity poll failed: %s — retrying", e)
            time.sleep(_POLL_INTERVAL_S)
            continue

        for act in (acts.get("results") or []):
            if activity_uuid and act.get("uuid") != activity_uuid:
                continue
            status = (act.get("status") or "").lower()
            processed = act.get("processed") or 0
            total = act.get("total") or 0
            if status in ("complete", "completed", "finished"):
                logger.info("DataSift skip-trace batch complete: %d/%d processed",
                            processed, total)
                return True
            # Sometimes 'status' isn't set but processed >= total signals done
            if total > 0 and processed >= total:
                return True

            elapsed = int(time.monotonic() - start)
            logger.info("  [%3ds] skip-trace batch status=%r processed=%d/%d",
                        elapsed, status, processed, total)
            break  # matched activity; keep polling

        time.sleep(_POLL_INTERVAL_S)

    logger.warning("DataSift skip-trace poll timed out after %ds — proceeding "
                   "anyway (records may have partial phones)", timeout_s)
    return False


def _score_phones(phones: list[str]) -> dict[str, str]:
    """Trestle-score every phone, return {normalized_phone: tier_name}.

    Uses phone_validator's single-phone caller so we don't have to wrap
    NoticeData objects. Costs ~$0.02/phone via Trestle Phone Intel API.
    """
    api_key = getattr(cfg, "TRESTLE_API_KEY", "")
    if not api_key:
        logger.warning("TRESTLE_API_KEY not set — cannot tier phones")
        return {}

    tier_map: dict[str, str] = {}
    for phone in phones:
        if not phone:
            continue
        result = pv.call_trestle(phone, api_key, add_litigator=False)
        # Trestle ALWAYS includes an "error" key set to None on success.
        # Prior bug: `if "error" in result` matched even when value was None,
        # causing every phone to be silently skipped ("continue"). Fix: check
        # truthiness of the value, not key presence.
        if result.get("error"):
            logger.debug("Trestle error for %s: %s", phone, result.get("error"))
            continue
        score = None
        # Trestle nests the score in a couple of places depending on plan
        if isinstance(result.get("activity_score"), (int, float)):
            score = int(result["activity_score"])
        elif isinstance(result.get("phone_intel"), dict):
            score = result["phone_intel"].get("activity_score")
        tier = pv.assign_tier(score, pv.DEFAULT_TIERS)
        tier_map[phone] = tier
    return tier_map


def _process_record(
    prop: dict, *, dry_run: bool = False,
) -> RecordResult:
    """Run the 3-vendor cascade + Trestle tagging on ONE record."""
    puuid = prop["uuid"]
    addr = ((prop.get("address") or {}).get("street") or "")[:40]
    result = RecordResult(property_uuid=puuid, address=addr)

    phones_before = _existing_phone_set(prop)
    result.phones_before = len(phones_before)

    if dry_run:
        logger.info("  [DRY] %s %s — %d phones, would run cascade",
                    puuid[:8], addr[:35], result.phones_before)
        result.action = "skipped"
        return result

    # ── Step 0: EXPLICITLY call Tracerfy on this record.
    # Previously this step only TAGGED existing phones as `src:tracerfy` on
    # the assumption Tracerfy ran upstream in the daily-sweep pipeline. That
    # holds for records scraped by our adapters but FAILS for records added
    # via bulk purchase or manually. So we now actually invoke Tracerfy here
    # unless the record already carries `traced_tracerfy` (idempotent).
    tracerfy_new_phones: set[str] = set()
    already_traced = _record_has_tag(prop, vt.RECORD_TAG_TRACED_TRACERFY)
    if already_traced:
        logger.debug("  Skipping Tracerfy — record already tagged traced_tracerfy")
        _tag_new_phones_with_source(puuid, phones_before, "tracerfy")
    else:
        try:
            import tracerfy_skip_tracer
            # Build a synthetic NoticeData from the DataSift record so we
            # can hand it to Tracerfy's batch API.
            notice = _build_notice_data_from_property(prop)
            if notice is not None:
                stats = tracerfy_skip_tracer.batch_skip_trace(
                    [notice], lookup_heir_addresses=False,
                )
                # Extract new phones from the returned NoticeData
                for slot in range(1, 10):
                    raw = getattr(notice, f"phone_{slot}", "") or ""
                    n = _norm_phone(raw)
                    if n and n not in phones_before:
                        tracerfy_new_phones.add(n)
                # Push new phones onto the DataSift owner
                if tracerfy_new_phones:
                    owner_uuid = (prop.get("owner") or {}).get("uuid")
                    if owner_uuid:
                        try:
                            ds._post(
                                f"/owner/{owner_uuid}/upsert-phones/",
                                {"phones": [
                                    {"number": n, "type": "UNKNOWN",
                                     "status": "UNKNOWN", "tags": []}
                                    for n in tracerfy_new_phones
                                ]},
                            )
                        except Exception as e:
                            logger.debug("tracerfy upsert-phones failed: %s", e)
                logger.info("  Tracerfy: %d new phones "
                            "(cost ~$%.2f, matched=%d)",
                            len(tracerfy_new_phones),
                            stats.get("cost", 0.0),
                            stats.get("matched", 0))
        except Exception as e:
            logger.warning("Tracerfy call failed for %s: %s", puuid[:8], e)
        try:
            vt.mark_vendor_traced(puuid, "tracerfy")
            _tag_new_phones_with_source(
                puuid, phones_before | tracerfy_new_phones, "tracerfy",
            )
        except Exception as e:
            logger.debug("tracerfy tag application failed for %s: %s", puuid[:8], e)

    # ── Step 1: Trigger DataSift native skip-trace ──
    # (Enformion runs later in this same function if not quota-blocked.)
    try:
        ds.skip_trace(puuid)
    except Exception as e:
        result.action = "error"
        result.error = f"skip_trace trigger: {e}"
        return result

    # ── Step 2: Wait for completion ──
    # NB: called ONCE per record in this loop. For a large batch it'd be
    # more efficient to call skip_trace for all N uuids up-front and then
    # poll one activity; that's a future optimization. Today: correctness > speed.
    _wait_for_activity_completion(None, expected_min_records=1, timeout_s=180)

    # ── Step 3: Re-fetch record, identify new phones ──
    try:
        refreshed = ds.get_property(puuid)
    except Exception as e:
        result.action = "error"
        result.error = f"re-fetch: {e}"
        return result

    phones_after_datasift = _existing_phone_set(refreshed)
    result.new_phones_datasift = len(phones_after_datasift - phones_before)

    # ── Tag traced_datasift + tag new phones with src:datasift ──
    try:
        vt.mark_vendor_traced(puuid, "datasift")
        _tag_new_phones_with_source(puuid, phones_after_datasift - phones_before, "datasift")
    except Exception as e:
        logger.debug("datasift tag application failed for %s: %s", puuid[:8], e)

    # ── Step 3.5: Enformion household search (paid tier, $0.10/skip) ──
    # Fires when we have an owner last-name + address on the record.
    # Enformion's household_search returns EVERY person at the address
    # (spouse, adult children living there, other relatives) — additive
    # to Tracerfy/DataSift which typically only find the primary owner.
    #
    # Preconditions:
    #   * Owner last-name resolved (Enformion needs LastName)
    #   * Full address (street + state + zip) present
    #   * Record is NOT on the Code Violation list (operator decision
    #     2026-07-28: skip Enformion on CV records since they're primarily
    #     door-knock / postcard outreach and heir-graph enrichment doesn't
    #     move the needle enough to justify $0.10/skip). DataSift native
    #     skip-trace + Trestle tiering still runs on CV records.
    owner = refreshed.get("owner") or {}
    owner_last = (owner.get("last_name") or "").strip()
    owner_addr = owner.get("address") or refreshed.get("address") or {}
    addr_street = (owner_addr.get("street") or "").strip()
    addr_city = (owner_addr.get("city") or "").strip()
    addr_state = (owner_addr.get("state") or "").strip()
    addr_zip = (owner_addr.get("postal_code") or "").strip()

    # Code Violation list UUID (probed 2026-07-28 via /list/)
    _CODE_VIOLATION_LIST_UUID = "c4d2bdeb-eb28-4276-a0e9-7b3c91b735e2"
    record_lists = refreshed.get("lists") or []
    is_code_violation = _CODE_VIOLATION_LIST_UUID in record_lists

    enformion_phones: set[str] = set()
    if is_code_violation:
        logger.debug("  Skipping Enformion (record is on Code Violation list)")
    elif (enf.is_configured() and owner_last and addr_street
            and addr_state and addr_zip):
        result.enformion_ran = True
        try:
            resp = enf.household_search(
                last_name=owner_last,
                street=addr_street, city=addr_city,
                state=addr_state, zip_code=addr_zip,
            )
        except Exception as e:
            logger.debug("Enformion call failed for %s: %s", puuid[:8], e)
            resp = {}

        # Extract phones from every returned person at the household
        for person in (resp.get("persons") or []):
            for phone_dict in enf.phones(person):
                num = _norm_phone(phone_dict.get("number") or "")
                if num and num not in phones_after_datasift and num not in phones_before:
                    enformion_phones.add(num)

        # If Enformion found new phones, attach them to the owner via
        # upsert-phones so they land on the record and get Trestle-scored
        if enformion_phones:
            try:
                owner_uuid = owner.get("uuid")
                if owner_uuid:
                    ds._post(
                        f"/owner/{owner_uuid}/upsert-phones/",
                        {"phones": [
                            {"number": n, "type": "MOBILE",
                             "status": "UNKNOWN", "tags": []}
                            for n in enformion_phones
                        ]},
                    )
                    logger.info("  Enformion added %d heir-household phones",
                                len(enformion_phones))
            except Exception as e:
                logger.warning("Failed to attach Enformion phones to owner: %s", e)

    phones_after = phones_after_datasift | enformion_phones
    result.phones_after = len(phones_after)
    result.new_phones_enformion = len(enformion_phones)

    # ── Tag traced_enformion regardless of whether Enformion ran (per operator
    # rule: attempted-vs-not is what the tag signals). Tag new phones as
    # src:enformion only when Enformion actually contributed them. ──
    try:
        vt.mark_vendor_traced(puuid, "enformion")
        _tag_new_phones_with_source(puuid, enformion_phones, "enformion")
    except Exception as e:
        logger.debug("enformion tag application failed for %s: %s", puuid[:8], e)

    if not phones_after:
        result.action = "noop"
        return result

    # ── Step 4: Trestle-score EVERY unique phone (belt + suspenders — some
    # existing phones may be untiered from prior runs) and tag each ──
    tier_map = _score_phones(sorted(phones_after))
    tier_counts: dict[str, int] = {}
    tagged = 0
    for phone, tier in tier_map.items():
        if tier == "Unknown":
            continue
        tier_uuid = ds.phone_tag_uuid(tier, create_if_missing=True)
        if not tier_uuid:
            continue
        try:
            ds.add_phone_tag(puuid, phone, [tier_uuid])
            tagged += 1
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        except Exception as e:
            logger.debug("add_phone_tag(%s, %s, %s) failed: %s",
                         puuid[:8], phone, tier, e)

    result.phones_tiered = tagged
    result.tier_distribution = tier_counts

    # ── Step 5: Append call-view summary to Notes (load-bearing UX per
    # operator feedback — Notes must always contain the full at-a-glance
    # snapshot the caller uses during outreach).
    #
    # EXCEPT: for probate-universe records where SmartSkip already wrote the
    # definitive family-tree Note, we skip this append per operator rule
    # ("smartskip output is your determining factor"). The check reads
    # traced_smartskip presence + smartskip_no_match absence (see
    # _record_has_smartskip_note_lock).
    if _record_has_smartskip_note_lock(refreshed):
        logger.debug("Skipping cascade Note write on %s — SmartSkip owns the Notes",
                     puuid[:8])
    else:
        tier_summary = ", ".join(f"{t}: {c}" for t, c in
                                 sorted(tier_counts.items(),
                                        key=lambda kv: pv.DEFAULT_TIERS.get(kv[0], (0,0))[1],
                                        reverse=True))
        if result.enformion_ran:
            enf_line = f"Enformion household new phones: {result.new_phones_enformion}"
        elif is_code_violation:
            enf_line = "Enformion: skipped (Code Violation record; heir enrichment disabled per operator policy)"
        else:
            enf_line = "Enformion: skipped (no owner name + address on record)"
        notes = (
            "\n=== SKIP-TRACE (3-vendor cascade) ===\n"
            f"Pre-cascade phones: {result.phones_before} (Tracerfy upstream)\n"
            f"DataSift new phones: {result.new_phones_datasift}\n"
            f"{enf_line}\n"
            f"Trestle scored {tagged} of {result.phones_after}: {tier_summary or 'none tiered'}\n"
        )
        try:
            ds.add_notes(puuid, notes)
        except Exception as e:
            logger.debug("add_notes on %s failed: %s", puuid[:8], e)

    # Clear queue_cascade tag if operator had queued this record — its work
    # is done, remove from the pending queue view.
    try:
        vt.clear_queue_cascade(puuid)
    except Exception as e:
        logger.debug("clear_queue_cascade failed for %s: %s", puuid[:8], e)

    result.action = "processed"
    return result


# ── CLI ─────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Three-vendor skip-trace cascade for newly-uploaded DataSift records"
    )
    ap.add_argument("--list", dest="list_name",
                    help="Only process records in this DataSift list "
                         "(e.g. 'Foreclosure', 'Probate'). Default: all lists.")
    ap.add_argument("--since-hours", type=int, default=24,
                    help="[Deprecated — retained for compat, no longer used] "
                         "Cascade now filters by tag absence, not timestamp.")
    ap.add_argument("--tag", dest="tag_filter",
                    help="Only process records carrying this tag. Use "
                         "'queue_cascade' to process operator-queued records. "
                         "Default: records lacking traced_tracerfy.")
    ap.add_argument("--max-records", type=int, default=200,
                    help="Safety cap on records processed per run (default: 200)")
    ap.add_argument("--property-uuid",
                    help="Process a single record by UUID (for testing)")
    ap.add_argument("--property-uuids",
                    help="Process multiple records — comma-separated UUIDs")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log decisions without spending API budget")
    ap.add_argument("--notify-slack", action="store_true",
                    help="Post cascade summary to SLACK_WEBHOOK_URL when done")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    if not ds.is_configured():
        logger.error("DATASIFT_API_KEY not set. Cannot proceed.")
        return 1

    since_dt = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    logger.info("═" * 72)
    logger.info("Thorough skip-trace cascade")
    logger.info("═" * 72)

    if args.property_uuids:
        uuids = [u.strip() for u in args.property_uuids.split(",") if u.strip()]
        logger.info("Multi-record mode: %d UUIDs", len(uuids))
        records = []
        for u in uuids:
            try:
                records.append(ds.get_property(u))
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", u[:8], e)
    elif args.property_uuid:
        logger.info("Single-record mode: %s", args.property_uuid)
        prop = ds.get_property(args.property_uuid)
        records = [prop]
    else:
        logger.info("List filter: %s", args.list_name or "(all)")
        logger.info("Tag filter:  %s", args.tag_filter or "(not-traced-yet — default)")
        records = _fetch_records(
            list_name=args.list_name,
            since_dt=since_dt,
            max_records=args.max_records,
            tag_filter=args.tag_filter,
        )
    logger.info("Records to process: %d", len(records))

    all_results: list[RecordResult] = []
    for i, rec in enumerate(records, 1):
        logger.info("── [%d/%d] %s @ %s ──",
                    i, len(records), rec["uuid"][:8],
                    ((rec.get("address") or {}).get("street") or "?")[:40])
        try:
            r = _process_record(rec, dry_run=args.dry_run)
        except Exception as e:
            r = RecordResult(
                property_uuid=rec["uuid"],
                action="error", error=f"unhandled: {e}",
            )
            logger.exception("Unhandled error on %s", rec["uuid"])
        all_results.append(r)

    # Summary
    logger.info("")
    logger.info("═" * 72)
    logger.info("CASCADE SUMMARY")
    logger.info("═" * 72)
    total_new_ds = sum(r.new_phones_datasift for r in all_results)
    total_new_enf = sum(r.new_phones_enformion for r in all_results)
    enformion_calls = sum(1 for r in all_results if r.enformion_ran)
    total_tiered = sum(r.phones_tiered for r in all_results)
    tier_totals: dict[str, int] = {}
    for r in all_results:
        for t, c in r.tier_distribution.items():
            tier_totals[t] = tier_totals.get(t, 0) + c
    actions: dict[str, int] = {}
    for r in all_results:
        actions[r.action] = actions.get(r.action, 0) + 1

    logger.info("Records:          %d", len(all_results))
    for act, ct in sorted(actions.items(), key=lambda kv: -kv[1]):
        logger.info("  · %-10s %d", act, ct)
    logger.info("New phones via DataSift native skip-trace: %d", total_new_ds)
    logger.info("New phones via Enformion household search: %d  (%d records called Enformion, ~$%.2f Enformion spend)",
                total_new_enf, enformion_calls, enformion_calls * 0.10)
    logger.info("Phones tiered via Trestle:                  %d", total_tiered)
    if tier_totals:
        logger.info("Tier distribution:")
        for tier in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"):
            if tier in tier_totals:
                logger.info("  · %-15s %d", tier, tier_totals[tier])
    logger.info("═" * 72)

    if args.notify_slack:
        # Build a Slack-formatted summary
        tier_lines = "\n".join(
            f"  · {t:<12} {tier_totals[t]}"
            for t in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
            if t in tier_totals
        )
        block = (
            "*🔎 Standard Cascade (Tracerfy → DataSift → Enformion → Trestle)*\n"
            f"  Records:                       {len(all_results)}\n"
            f"  DataSift native new phones:    {total_new_ds}\n"
            f"  Enformion new phones:          {total_new_enf} "
            f"(${enformion_calls * 0.10:.2f} across {enformion_calls} records)\n"
            f"  Phones Trestle-tiered:         {total_tiered}\n"
        )
        if tier_lines:
            block += f"  Tier distribution:\n{tier_lines}\n"
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
            from slack_notifier import _send_webhook
            _send_webhook(block)
            logger.info("Cascade summary posted to Slack")
        except Exception as e:
            logger.warning("Slack post failed: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
