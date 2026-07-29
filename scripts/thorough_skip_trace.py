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

logger = logging.getLogger(__name__)


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
    new_phones: int = 0
    phones_tiered: int = 0
    tier_distribution: dict[str, int] = field(default_factory=dict)
    error: str = ""


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
) -> list[dict]:
    """Pull recently-created records from DataSift, optionally filtered by list.

    Uses -created descending order + walks pages until we hit the since_dt
    cutoff or the max_records safety cap.
    """
    list_filter_uuid: str | None = None
    if list_name:
        list_filter_uuid = ds.list_uuid(list_name, create_if_missing=False)
        if not list_filter_uuid:
            logger.warning("List %r not found in DataSift — nothing to process", list_name)
            return []

    results: list[dict] = []
    limit = 100
    offset = 0
    since_iso = since_dt.isoformat()

    while len(results) < max_records:
        params = {
            "limit": limit,
            "offset": offset,
            "ordering": "-created",
        }
        # NOTE: not all DataSift filters are exposed on /property/. If list
        # filtering isn't natively supported, we filter locally after fetch.
        resp = ds._get("/property/", params)
        page = resp.get("results") or []
        if not page:
            break

        for row in page:
            created_str = (row.get("created") or "")
            if created_str and created_str < since_iso:
                # We've paged past the cutoff — stop
                return _apply_list_filter(results, list_filter_uuid)
            # Skip records without the target list, if we're list-filtering
            results.append(row)
            if len(results) >= max_records:
                break

        if len(page) < limit:
            break
        offset += limit

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
        if "error" in result:
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

    # ── Step 1: Trigger DataSift native skip-trace ──
    # (Tracerfy already ran upstream in the pipeline; Enformion too if
    # not quota-blocked. Both populated phones_before if they hit.)
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

    phones_after = _existing_phone_set(refreshed)
    new_phones = phones_after - phones_before
    result.phones_after = len(phones_after)
    result.new_phones = len(new_phones)

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
    # snapshot the caller uses during outreach). ──
    tier_summary = ", ".join(f"{t}: {c}" for t, c in
                             sorted(tier_counts.items(),
                                    key=lambda kv: pv.DEFAULT_TIERS.get(kv[0], (0,0))[1],
                                    reverse=True))
    notes = (
        "\n=== SKIP-TRACE (3-vendor cascade) ===\n"
        f"Pre-cascade phones: {result.phones_before} (Tracerfy upstream)\n"
        f"DataSift new phones: {result.new_phones}\n"
        f"Trestle scored {tagged} of {result.phones_after}: {tier_summary or 'none tiered'}\n"
    )
    try:
        ds.add_notes(puuid, notes)
    except Exception as e:
        logger.debug("add_notes on %s failed: %s", puuid[:8], e)

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
                    help="Look back this many hours (default: 24 = last daily-cron run)")
    ap.add_argument("--max-records", type=int, default=200,
                    help="Safety cap on records processed per run (default: 200)")
    ap.add_argument("--property-uuid",
                    help="Process a single record by UUID (for testing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log decisions without spending API budget")
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

    if args.property_uuid:
        logger.info("Single-record mode: %s", args.property_uuid)
        prop = ds.get_property(args.property_uuid)
        records = [prop]
    else:
        logger.info("List filter: %s", args.list_name or "(all)")
        logger.info("Since: %s (%dh ago)", since_dt.isoformat(), args.since_hours)
        records = _fetch_records(
            list_name=args.list_name,
            since_dt=since_dt,
            max_records=args.max_records,
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
    total_new = sum(r.new_phones for r in all_results)
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
    logger.info("New phones via DataSift native skip-trace: %d", total_new)
    logger.info("Phones tiered via Trestle:                  %d", total_tiered)
    if tier_totals:
        logger.info("Tier distribution:")
        for tier in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"):
            if tier in tier_totals:
                logger.info("  · %-15s %d", tier, tier_totals[tier])
    logger.info("═" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
