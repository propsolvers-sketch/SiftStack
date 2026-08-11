"""Probate-universe cascade — SmartSkip-first, 4-vendor enrichment.

Records in the probate universe (notice_type ∈ {probate, pre_probate} OR in
Obituary/Probate/Pre-Probate-Deceased lists) run through:

  1. SmartSkip   — this script — extracts owner phones + family tree +
                   creates heir records + writes definitive Notes entry
  2. Tracerfy    — thorough_skip_trace.py (upstream in daily pipeline)
  3. DataSift    — thorough_skip_trace.py
  4. Enformion   — thorough_skip_trace.py
  5. Trestle     — thorough_skip_trace.py — scores union of all 4 vendors'
                   phones, applies Dial-* tier tags

This script handles step 1 only. Downstream steps run via the existing
thorough_skip_trace.py which reads the `smartskip_no_match` tag to decide
whether to append its own summary Note or preserve SmartSkip's.

Note ownership rules:
  * SmartSkip HAS data      → writes rich family-tree Note. Later vendors
                              MUST NOT append (checked via `smartskip_no_match`
                              tag being absent + `traced_smartskip` present).
  * SmartSkip has NO data   → tags `smartskip_no_match`. Later vendors append
                              their normal summary Note as if SmartSkip
                              hadn't run.
  * SmartSkip session down  → tags `smartskip_deferred`. Downstream vendors
                              proceed normally; next daily sweep re-attempts.

Usage:
  # Full daily submit for probate/pre-probate/obituary universe
  python scripts/probate_cascade.py

  # Rehash-only mode: only records NOT yet tagged traced_smartskip
  python scripts/probate_cascade.py --rehash-only

  # Dry-run: build the CSV + log what would submit, but don't upload/pay
  python scripts/probate_cascade.py --dry-run

  # Small batch cap for testing
  python scripts/probate_cascade.py --max-records 5
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds
import enrichment_router as router
import vendor_tags as vt
import smartskip_client as ss

logger = logging.getLogger(__name__)


def _relationship_phone_tag(role: str) -> str | None:
    """Return UUID of a `rel:<role>` phone tag, creating on demand.

    Normalizes SmartSkip's role labels to consistent kebab-case:
      "Spouse"       → rel:spouse
      "Child"        → rel:child
      "In-law"       → rel:in-law
      "Other Relative" → rel:other-relative
      "Past neighbor"  → rel:past-neighbor
      "Coworker"     → rel:coworker
      "Friend"       → rel:friend
      "owner"        → rel:owner
    """
    if not role:
        return None
    norm = role.strip().lower().replace(" ", "-").replace("_", "-")
    tag_name = f"rel:{norm}"
    try:
        return ds.phone_tag_uuid(tag_name, create_if_missing=True)
    except Exception as e:
        logger.debug("Failed to resolve/create phone tag %r: %s", tag_name, e)
        return None


def _apply_phone_tags(property_uuid: str, phone_number: str,
                      *tag_uuids: str) -> None:
    """Apply one or more tag UUIDs to a phone number.

    Batches the tag UUIDs into ONE API call (rather than N sequential calls)
    to avoid triggering DataSift's per-endpoint rate limiter (429). Ignores
    failures — tag application is best-effort, phones are still on record.
    """
    real_uuids = [u for u in tag_uuids if u]
    if not real_uuids:
        return
    try:
        # add_phone_tag accepts a LIST of tag UUIDs — one API call per phone
        # instead of N calls per phone
        ds.add_phone_tag(property_uuid, phone_number, real_uuids)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ProbateCascadeSummary:
    """Aggregate metrics for the SmartSkip pass — feeds daily Slack post."""
    submitted: int = 0
    matched: int = 0
    no_match: int = 0
    heirs_created: int = 0
    heir_creation_errors: int = 0
    deferred: int = 0                 # session expired / payment failure / timeout
    deferral_reason: str = ""
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    dry_run: bool = False
    # Optional: results from the chained standard 3-vendor cascade
    standard_cascade_ran: bool = False
    standard_cascade_results: dict = field(default_factory=dict)

    def to_slack_block(self) -> str:
        """Formatted markdown-ish block for Slack daily post."""
        lines = ["📞 *SmartSkip Enrichment (probate/pre-probate/obituary universe)*"]
        if self.dry_run:
            lines.append("  _(DRY RUN — no records submitted)_")
        if self.deferred:
            lines.append(f"  ⚠ DEFERRED: {self.deferred} records — {self.deferral_reason}")
            lines.append(f"  Records queued for tomorrow's retry (tagged `smartskip_deferred`)")
            return "\n".join(lines)
        lines.append(f"  Submitted:   {self.submitted} records")
        rate = f"({100 * self.matched / self.submitted:.0f}%)" if self.submitted else ""
        lines.append(f"  Matched:     {self.matched} records {rate}")
        lines.append(f"  No match:    {self.no_match} records → tagged `smartskip_no_match`")
        lines.append(f"  Heirs added: {self.heirs_created} heir records"
                     + (f" ({self.heir_creation_errors} errors)" if self.heir_creation_errors else ""))
        lines.append(f"  Cost:        ${self.cost_usd:.2f} (${0.15:.2f} × {self.submitted})")
        lines.append(f"  Duration:    {self.duration_seconds / 60:.1f}m")

        # Chained standard cascade section (only when --and-standard-cascade ran)
        if self.standard_cascade_ran and self.standard_cascade_results:
            r = self.standard_cascade_results
            lines.append("")
            lines.append("🔎 *Standard Cascade (Tracerfy → DataSift native → Enformion → Trestle)*")
            lines.append(f"  Records processed:             {r.get('records', 0)}")
            lines.append(f"  DataSift native new phones:    {r.get('new_phones_datasift', 0)}")
            enf_cost = r.get('enformion_cost', 0.0)
            lines.append(f"  Enformion new phones:          {r.get('new_phones_enformion', 0)} "
                         f"(${enf_cost:.2f} across {r.get('enformion_calls', 0)} records)")
            lines.append(f"  Phones Trestle-tiered:         {r.get('phones_tiered', 0)}")
            tier_dist = r.get("tier_distribution", {})
            if tier_dist:
                lines.append("  Tier distribution:")
                for tier in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"):
                    if tier in tier_dist:
                        lines.append(f"    · {tier:<12} {tier_dist[tier]}")
            total_cost = self.cost_usd + enf_cost
            lines.append("")
            lines.append(f"💰 *Total spend this batch: ${total_cost:.2f}* "
                         f"(SmartSkip ${self.cost_usd:.2f} + Enformion ${enf_cost:.2f})")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Record → SmartSkipRow adapter
# ─────────────────────────────────────────────────────────────────────


def _record_to_smartskip_row(record: dict) -> ss.SmartSkipRow | None:
    """Extract SmartSkip submission fields from a DataSift record.

    Skips records missing any of the required 3 fields (First Name, Last Name,
    Mailing Address). Returns None in that case.
    """
    owner = record.get("owner") or {}
    first = (owner.get("first_name") or "").strip()
    last = (owner.get("last_name") or "").strip()

    owner_addr = owner.get("address") or {}
    mailing_street = (owner_addr.get("street") or "").strip()
    if not (first and last and mailing_street):
        return None

    prop_addr = record.get("address") or {}
    return ss.SmartSkipRow(
        first_name=first,
        last_name=last,
        mailing_address=mailing_street,
        middle_name="",   # DataSift owner schema doesn't split middle
        mailing_city=(owner_addr.get("city") or "").strip(),
        mailing_state=(owner_addr.get("state") or "").strip(),
        mailing_zip=(owner_addr.get("zip5") or owner_addr.get("postal_code") or "").strip()[:5],
        property_address=(prop_addr.get("street") or "").strip(),
        property_city=(prop_addr.get("city") or "").strip(),
        property_state=(prop_addr.get("state") or "").strip(),
        property_zip=(prop_addr.get("zip5") or prop_addr.get("postal_code") or "").strip()[:5],
        external_id=record.get("uuid", ""),
    )


# ─────────────────────────────────────────────────────────────────────
# Heir creation
# ─────────────────────────────────────────────────────────────────────


def _create_heir_record(
    parent_record: dict,
    heir: ss.SmartSkipContact,
    notice_type: str,
    phone_tiers: dict[str, str] | None = None,
) -> str | None:
    """Create a new DataSift record for a signing-authority heir.

    Uses the heir's OWN mailing address (their residence) as the property
    address — NOT the deceased's property. This is both more accurate
    (heirs typically live at their own homes) and avoids the "Property
    address already exists" 400 error that fires when we try to create
    a second property record at the same address as the parent.

    Skips heirs without an address on file — we need SOMEWHERE to anchor
    the property record. Their info still appears in the SmartSkip note
    on the parent record.

    Tags the heir record with heir_of_<notice_type> so DataSift filter
    presets can isolate the heirs audience.

    Returns the new property UUID, or None if creation failed / skipped.
    """
    if not heir.first_name and not heir.last_name:
        return None
    if not heir.address:
        # No mailing address on file — can't create a property record.
        # Heir info still lives in the parent record's SmartSkip note.
        logger.debug("Skipping heir %s %s — no mailing address",
                     heir.first_name, heir.last_name)
        return None

    try:
        # Create heir record at HEIR'S OWN address. If that address already
        # exists in DataSift (heir is already in the system), we catch the
        # error and add tags to the existing record.
        created = ds.create_property(
            street=heir.address,
            city=heir.city,
            state=heir.state,
            postal_code=(heir.zip or "")[:5],
            owner_first=heir.first_name,
            owner_last=heir.last_name,
            # Mailing == property for heirs (their residence IS the mailing target)
            mailing_street=heir.address,
            mailing_city=heir.city,
            mailing_state=heir.state,
            mailing_zip=heir.zip,
            phones=[{"number": p["number"], "type": p.get("type", "")} for p in heir.phones],
            emails=list(heir.emails),
        )
        heir_uuid = created.get("uuid")
        if not heir_uuid:
            return None
    except Exception as e:
        # DataSift returns 400 "Property address already exists" if the
        # heir's address is already in the account. Parse the error to grab
        # the existing property UUID and add heir tags to it instead.
        err_str = str(e)
        if "already exists" in err_str.lower() and '"property"' in err_str:
            import re as _re
            m = _re.search(r'"property":\s*\[\s*"([0-9a-f-]{36})"', err_str)
            if m:
                heir_uuid = m.group(1)
                logger.debug("Heir %s %s already exists in DataSift (uuid=%s) — tagging existing",
                             heir.first_name, heir.last_name, heir_uuid[:8])
            else:
                logger.warning("Heir already-exists error but couldn't parse property UUID: %s",
                               err_str[:200])
                return None
        else:
            logger.warning("Failed to create heir record for %s %s: %s",
                           heir.first_name, heir.last_name, e)
            return None

    # Apply heir_of_<notice_type> + traced_smartskip tags to the RECORD
    tags_to_add = []
    heir_tag = ds.tag_uuid(f"heir_of_{notice_type}", create_if_missing=True)
    if heir_tag:
        tags_to_add.append(heir_tag)
    smartskip_tag = vt.traced_record_tag_uuid("smartskip")
    tags_to_add.append(smartskip_tag)
    if tags_to_add:
        try:
            ds.add_tags(heir_uuid, tags_to_add)
        except Exception as e:
            logger.debug("add_tags on heir %s failed: %s", heir_uuid[:8], e)

    # Tag each PHONE on the heir record with:
    #   src:smartskip · rel:<role> · Dial <tier>
    # so operator sees WHO they're calling AND how good the number is at a glance.
    if heir.phones:
        src_smartskip = vt.src_phone_tag_uuid("smartskip")
        rel_tag = _relationship_phone_tag(heir.role)
        import thorough_skip_trace as tst
        tiers = phone_tiers or {}
        for phone in heir.phones:
            digits = tst._norm_phone(phone["number"])
            tier_name = tiers.get(digits, "")
            tier_uuid = (
                ds.phone_tag_uuid(tier_name, create_if_missing=True)
                if tier_name and tier_name != "Unknown" else None
            )
            _apply_phone_tags(
                heir_uuid, phone["number"], src_smartskip, rel_tag, tier_uuid,
            )

    return heir_uuid


# ─────────────────────────────────────────────────────────────────────
# Apply SmartSkip result to a DataSift record
# ─────────────────────────────────────────────────────────────────────


def _apply_smartskip_result(
    record: dict,
    result: ss.SmartSkipResult,
    scraped_date: str,
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Merge SmartSkip result into an existing DataSift record.

    Returns (heirs_created_count, heir_error_count).
    """
    property_uuid = record["uuid"]
    notice_type = router._notice_type_of(record) or "probate"

    if dry_run:
        n_heirs = len(result.heirs) if result.has_data else 0
        logger.info("[dry-run] would apply SmartSkip result to %s: %d heirs, has_data=%s",
                    property_uuid[:8], n_heirs, result.has_data)
        return (n_heirs, 0)

    # ── 1. Always tag traced_smartskip so downstream knows SmartSkip was attempted
    vt.mark_vendor_traced(property_uuid, "smartskip")

    if not result.has_data:
        # No family tree, no phones — tag as no_match so standard cascade
        # note-writing behaves normally
        vt.mark_state(property_uuid, vt.RECORD_TAG_SMARTSKIP_NO_MATCH)
        logger.info("SmartSkip no-match for %s", property_uuid[:8])
        return (0, 0)

    # ── 1a. Trestle-score ALL phones (owner + heirs + extended family) so we can
    # embed the Dial-tier in the note AND apply the tier tag to each phone.
    # One batched Trestle pass, ~$0.02/phone.
    all_phone_numbers: list[str] = []
    for p in result.owner.phones:
        all_phone_numbers.append(p["number"])
    for rel in result.relatives + result.associates:
        for p in rel.phones:
            all_phone_numbers.append(p["number"])
    phone_tiers: dict[str, str] = {}
    if all_phone_numbers:
        try:
            import thorough_skip_trace as tst
            # dedupe + normalize to 10-digit form for the tier lookup map
            unique_phones = list({tst._norm_phone(n) for n in all_phone_numbers if n})
            unique_phones = [p for p in unique_phones if p]
            phone_tiers = tst._score_phones(sorted(unique_phones))
            logger.info("Trestled %d unique SmartSkip phones (%d tiers assigned)",
                        len(unique_phones), len(phone_tiers))
        except Exception as e:
            logger.warning("Trestle scoring failed: %s — note will lack tiers", e)

    # ── 1b. Extract Courthouse / Probate context from the property record.
    # Pulls the built-in fields (probate_open_date, personal_representative,
    # attorney_on_file, last_obituary_date, foreclosure_date). If additional
    # custom fields exist (probate_case_number, obituary_url, decedent_name),
    # they can be added by pulling from ds custom-fields API later.
    def _s(v):
        """Format non-empty scalar for display, or '' if empty/None."""
        if v is None or v == "":
            return ""
        return str(v).strip()

    probate_context = {
        "probate_open_date":              _s(record.get("probate_open_date")),
        "personal_representative":         _s(record.get("personal_representative")),
        "personal_representative_phone":   _s(record.get("personal_representative_phone")),
        "attorney_on_file":                _s(record.get("attorney_on_file")),
        "last_obituary_date":              _s(record.get("last_obituary_date")),
        "foreclosure_date":                _s(record.get("foreclosure_date")),
        # SmartSkip-supplied: whether the owner is flagged as deceased in
        # their consumer-data database (independent of court probate filing).
        "smartskip_deceased_flag":         "Yes" if result.owner.deceased else "",
        # These fields come from custom-field values that require a separate
        # DataSift API call to retrieve. Left empty for now — operator can
        # extend if needed. Placeholder keys documented for future use:
        # "decedent_name", "date_of_death", "obituary_url",
        # "probate_case_number", "judge_of_probate", "hearing_date",
        # "creditor_claim_deadline", "notice_type", "county", "source_url",
    }
    # Drop empty keys so the note-format helper won't render empty rows
    probate_context = {k: v for k, v in probate_context.items() if v}
    if probate_context:
        logger.debug("Populated %d probate context fields for note",
                     len(probate_context))

    # ── 2. Write the definitive family-tree Note (sole entry per operator rule)
    # All records reaching probate_cascade are in the probate universe (per
    # enrichment_router.query_probate_universe_records), so we always pass
    # is_probate_record=True to make the Courthouse section render — even if
    # the record has no source metadata, the operator sees a clear "no data
    # on file" message explaining WHY the section is empty.
    note = ss.format_note(
        result, scraped_date,
        phone_tiers=phone_tiers,
        probate_context=probate_context,
        is_probate_record=True,
    )
    try:
        ds.add_notes(property_uuid, note)
    except Exception as e:
        logger.warning("add_notes failed on %s: %s", property_uuid[:8], e)

    # ── 3. Upsert owner phones + emails into the actual phone/email slots
    # so Trestle scores them + filter presets can see them.
    # Uses the same endpoint Enformion uses in thorough_skip_trace.py.
    # DataSift supports up to 30 phone slots — well beyond our worst case
    # (Tracerfy + DataSift + Enformion + SmartSkip ≈ 15-20 phones max).
    owner = record.get("owner") or {}
    owner_uuid = owner.get("uuid")
    if owner_uuid and result.owner.phones:
        try:
            ds._post(
                f"/owner/{owner_uuid}/upsert-phones/",
                {"phones": [
                    {"number": p["number"],
                     "type": (p.get("type") or "UNKNOWN").upper(),
                     # DataSift's valid status enum does NOT include "connected"
                     # (verified 2026-08-06 via HTTP 400 response). "UNKNOWN"
                     # is always accepted — Trestle will re-classify each phone
                     # via activity_score downstream anyway.
                     "status": "UNKNOWN",
                     "tags": []}
                    for p in result.owner.phones
                ]},
            )
            logger.info("Upserted %d SmartSkip owner phones on %s",
                        len(result.owner.phones), property_uuid[:8])
        except Exception as e:
            logger.warning("upsert-phones failed on %s: %s", property_uuid[:8], e)

    if owner_uuid and result.owner.emails:
        try:
            ds._post(
                f"/owner/{owner_uuid}/upsert-emails/",
                {"emails": [{"address": e, "tags": []} for e in result.owner.emails]},
            )
            logger.debug("Upserted %d SmartSkip owner emails", len(result.owner.emails))
        except Exception as e:
            logger.debug("upsert-emails failed on %s: %s", property_uuid[:8], e)

    # After phone upsert, tag each phone with:
    #   src:smartskip  (source vendor attribution)
    #   rel:owner      (relationship — helps operator prioritize during calls)
    #   Dial <tier>    (Trestle-scored tier from the pass above)
    try:
        src_smartskip = vt.src_phone_tag_uuid("smartskip")
        rel_owner = _relationship_phone_tag("owner")
        for phone_dict in result.owner.phones:
            import thorough_skip_trace as tst
            digits = tst._norm_phone(phone_dict["number"])
            tier_name = phone_tiers.get(digits, "")
            tier_uuid = ds.phone_tag_uuid(tier_name, create_if_missing=True) if tier_name and tier_name != "Unknown" else None
            _apply_phone_tags(
                property_uuid, phone_dict["number"],
                src_smartskip, rel_owner, tier_uuid,
            )
    except Exception as e:
        logger.debug("smartskip src/rel/tier tag apply failed on %s: %s", property_uuid[:8], e)

    # ── 4. Create heir records for signing-authority relatives (Spouse/Child/In-law)
    heirs_created = 0
    heir_errors = 0
    for heir in result.heirs:
        heir_uuid = _create_heir_record(record, heir, notice_type, phone_tiers=phone_tiers)
        if heir_uuid:
            heirs_created += 1
        else:
            heir_errors += 1

    return (heirs_created, heir_errors)


# ─────────────────────────────────────────────────────────────────────
# Main orchestration
# ─────────────────────────────────────────────────────────────────────


def _merge_manual_result(
    *, result_csv: Path, and_standard_cascade: bool = False,
) -> ProbateCascadeSummary:
    """Merge a manually-downloaded SmartSkip result CSV into DataSift.

    Bypasses all Playwright. Uses the paired submission CSV in
    outbox/smartskip/submitted/ to reconstruct the row → property UUID
    mapping via First Name + Last Name + Mailing Address match against
    the current probate universe.
    """
    started = time.time()
    summary = ProbateCascadeSummary()
    vt.ensure_all_tags_exist()

    if not result_csv.exists():
        logger.error("Result CSV not found: %s", result_csv)
        summary.deferral_reason = f"result csv not found: {result_csv.name}"
        return summary

    # Locate the paired submission CSV (same name minus "result_" prefix)
    submitted_dir = Path(__file__).parent.parent / "outbox" / "smartskip" / "submitted"
    stem = result_csv.name
    if stem.startswith("result_"):
        stem = stem[len("result_"):]
    submission_csv = submitted_dir / stem
    if not submission_csv.exists():
        logger.error("Paired submission CSV not found: %s", submission_csv)
        summary.deferral_reason = f"paired submission csv not found: {stem}"
        return summary

    logger.info("Merging manually-downloaded result: %s", result_csv.name)
    logger.info("Reconstructing row → record mapping from %s", submission_csv.name)

    records = router.query_probate_universe_records(limit=500)
    row_to_record: dict[int, dict] = {}
    import csv as _csv
    with submission_csv.open("r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for idx, row in enumerate(reader):
            key = (
                (row.get("First Name") or "").strip().lower(),
                (row.get("Last Name") or "").strip().lower(),
                (row.get("Mailing Address") or "").strip().lower(),
            )
            for rec in records:
                owner = rec.get("owner") or {}
                owner_addr = owner.get("address") or {}
                rec_key = (
                    (owner.get("first_name") or "").strip().lower(),
                    (owner.get("last_name") or "").strip().lower(),
                    (owner_addr.get("street") or "").strip().lower(),
                )
                if rec_key == key:
                    row_to_record[idx] = rec
                    break

    logger.info("Reconstructed %d row → record mappings", len(row_to_record))
    scraped_date = time.strftime("%Y-%m-%d")

    for result in ss.parse_result_csv(result_csv):
        if result.row_index not in row_to_record:
            logger.warning("Row %d has no matched record — skipping merge",
                           result.row_index)
            continue
        parent_record = row_to_record[result.row_index]
        heirs, errors = _apply_smartskip_result(parent_record, result, scraped_date)
        summary.submitted += 1
        summary.heirs_created += heirs
        summary.heir_creation_errors += errors
        if result.has_data:
            summary.matched += 1
        else:
            summary.no_match += 1

    summary.cost_usd = 0.15 * summary.submitted
    summary.duration_seconds = time.time() - started

    # ── Upload SmartSkip result CSV to Dropbox alongside daily-sweep archives.
    # Same uploader daily_finalize.py uses. Non-fatal — a Dropbox miss shouldn't
    # break the merge run (result is already in outbox/ + DataSift).
    try:
        from dropbox_archive_uploader import upload_files as _dbx_upload
        dbx_results = _dbx_upload([result_csv])
        for r in dbx_results:
            if r.get("success"):
                logger.info("Uploaded SmartSkip result to Dropbox: %s", r.get("dropbox_path"))
            else:
                logger.warning("Dropbox upload failed for %s: %s",
                               r.get("path"), r.get("error"))
    except Exception as e:
        logger.warning("Dropbox upload skipped: %s", e)

    # ── OPTIONAL: chain into the standard 3-vendor cascade for these records ─
    if and_standard_cascade and row_to_record:
        logger.info("")
        logger.info("Chaining into standard cascade for %d records...", len(row_to_record))
        uuids = [rec["uuid"] for rec in row_to_record.values()]
        summary.standard_cascade_ran = True
        try:
            summary.standard_cascade_results = _invoke_standard_cascade(uuids)
        except Exception as e:
            logger.warning("Standard cascade failed: %s", e)

    return summary


def _invoke_standard_cascade(uuids: list[str]) -> dict:
    """Run thorough_skip_trace on the given UUIDs, return aggregate metrics."""
    import thorough_skip_trace as tst

    all_results = []
    for uuid in uuids:
        try:
            prop = ds.get_property(uuid)
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", uuid[:8], e)
            continue
        try:
            r = tst._process_record(prop, dry_run=False)
            all_results.append(r)
            logger.info("  Standard cascade %s: action=%s new_ds=%d new_enf=%d tiered=%d",
                        uuid[:8], r.action, r.new_phones_datasift,
                        r.new_phones_enformion, r.phones_tiered)
        except Exception as e:
            logger.exception("Unhandled error on %s", uuid[:8])

    total_new_ds = sum(r.new_phones_datasift for r in all_results)
    total_new_enf = sum(r.new_phones_enformion for r in all_results)
    enformion_calls = sum(1 for r in all_results if r.enformion_ran)
    total_tiered = sum(r.phones_tiered for r in all_results)
    tier_totals: dict[str, int] = {}
    for r in all_results:
        for t, c in r.tier_distribution.items():
            tier_totals[t] = tier_totals.get(t, 0) + c

    return {
        "records": len(all_results),
        "new_phones_datasift": total_new_ds,
        "new_phones_enformion": total_new_enf,
        "enformion_calls": enformion_calls,
        "enformion_cost": enformion_calls * 0.10,
        "phones_tiered": total_tiered,
        "tier_distribution": tier_totals,
    }


def _recover_paid_batch(
    *, csv_name: str, headless: bool = True,
) -> ProbateCascadeSummary:
    """Recover a batch that was submitted + paid but crashed on Step 5.

    Reads the local submission CSV to reconstruct the row → record mapping,
    then polls SmartSkip's Skips History for the completed batch and
    downloads + merges the results.
    """
    started = time.time()
    summary = ProbateCascadeSummary()

    vt.ensure_all_tags_exist()

    # Locate the submission CSV so we can read the row → external_id mapping
    submitted_dir = Path(__file__).parent.parent / "outbox" / "smartskip" / "submitted"
    csv_path = submitted_dir / csv_name
    if not csv_path.exists():
        logger.error("Recovery CSV not found: %s", csv_path)
        summary.deferred = 0
        summary.deferral_reason = f"csv not found: {csv_name}"
        return summary

    # Re-derive row_index → external_id (property UUID) mapping
    # by re-querying the probate universe + matching on First+Last name +
    # Mailing Address.
    logger.info("Recovering batch %s", csv_name)
    logger.info("Re-fetching probate universe records to reconstruct row→uuid mapping...")
    records = router.query_probate_universe_records(limit=500)
    row_to_record: dict[int, dict] = {}
    import csv as _csv
    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for idx, row in enumerate(reader):
            key = (
                (row.get("First Name") or "").strip().lower(),
                (row.get("Last Name") or "").strip().lower(),
                (row.get("Mailing Address") or "").strip().lower(),
            )
            for rec in records:
                owner = rec.get("owner") or {}
                owner_addr = owner.get("address") or {}
                rec_key = (
                    (owner.get("first_name") or "").strip().lower(),
                    (owner.get("last_name") or "").strip().lower(),
                    (owner_addr.get("street") or "").strip().lower(),
                )
                if rec_key == key:
                    row_to_record[idx] = rec
                    break

    logger.info("Reconstructed %d/%d row→record mappings", len(row_to_record),
                sum(1 for _ in csv_path.open("r", encoding="utf-8-sig")) - 1)

    # Build a fake SmartSkipBatch just for wait_and_download
    batch = ss.SmartSkipBatch(
        batch_id=csv_name,
        row_count=len(row_to_record),
        submitted_at=csv_name,  # non-critical for recovery
        csv_path=csv_path,
        external_ids=[row_to_record.get(i, {}).get("uuid", "")
                      for i in range(len(row_to_record) + 10)],
    )

    scraped_date = time.strftime("%Y-%m-%d")

    with ss.SmartSkipClient(headless=headless) as client:
        try:
            result_csv_path = client.wait_and_download(batch)
        except (ss.SmartSkipTimeoutError, ss.SmartSkipSessionExpired) as e:
            summary.deferred = batch.row_count
            summary.deferral_reason = f"recovery failed: {e}"
            summary.duration_seconds = time.time() - started
            return summary

    logger.info("Downloaded: %s", result_csv_path)

    # Parse + apply as usual
    summary.submitted = batch.row_count
    summary.cost_usd = 0.15 * summary.submitted
    for result in ss.parse_result_csv(result_csv_path):
        if result.row_index not in row_to_record:
            logger.warning("Row %d has no matched record — skipping merge",
                           result.row_index)
            continue
        parent_record = row_to_record[result.row_index]
        heirs, errors = _apply_smartskip_result(parent_record, result, scraped_date)
        summary.heirs_created += heirs
        summary.heir_creation_errors += errors
        if result.has_data:
            summary.matched += 1
        else:
            summary.no_match += 1

    summary.duration_seconds = time.time() - started
    return summary


def run_probate_cascade(
    *,
    rehash_only: bool = False,
    max_records: int | None = None,
    dry_run: bool = False,
    headless: bool = True,
) -> ProbateCascadeSummary:
    """Full SmartSkip pass over the probate universe."""
    started = time.time()
    summary = ProbateCascadeSummary(dry_run=dry_run)

    # Ensure vendor + state tags exist before we start applying them
    vt.ensure_all_tags_exist()

    # ── 1. Query probate universe records
    logger.info("Querying probate universe records...")
    records = router.query_probate_universe_records(
        require_traced_smartskip_missing=rehash_only,
        limit=500,
    )
    if max_records:
        records = records[:max_records]
    logger.info("Fetched %d records", len(records))

    # ── 2. Convert to submission rows, dropping records missing required fields
    rows_and_records: list[tuple[ss.SmartSkipRow, dict]] = []
    skipped_bad_fields = 0
    for rec in records:
        row = _record_to_smartskip_row(rec)
        if row is None:
            skipped_bad_fields += 1
            continue
        rows_and_records.append((row, rec))
    logger.info("Prepared %d rows (%d dropped for missing required fields)",
                len(rows_and_records), skipped_bad_fields)

    if not rows_and_records:
        logger.info("No submittable rows — exiting")
        summary.duration_seconds = time.time() - started
        return summary

    summary.submitted = len(rows_and_records)
    summary.cost_usd = 0.15 * summary.submitted

    if dry_run:
        logger.info("[dry-run] Would submit %d rows for $%.2f",
                    summary.submitted, summary.cost_usd)
        summary.duration_seconds = time.time() - started
        return summary

    # ── 3. Submit batch to SmartSkip via Playwright, wait, download
    scraped_date = time.strftime("%Y-%m-%d")
    batch_label = f"Probate_Cascade_{scraped_date}"

    try:
        with ss.SmartSkipClient(headless=headless) as client:
            rows = [rp[0] for rp in rows_and_records]
            batch = client.submit_batch(rows, batch_label=batch_label)
            logger.info("SmartSkip batch submitted: %s", batch.batch_id)

            result_csv_path = client.wait_and_download(batch)
            logger.info("SmartSkip result downloaded: %s", result_csv_path)
    except ss.SmartSkipSessionExpired as e:
        summary.deferred = summary.submitted
        summary.deferral_reason = "session expired — operator re-login required"
        summary.submitted = 0
        for _, rec in rows_and_records:
            try:
                vt.mark_state(rec["uuid"], vt.RECORD_TAG_SMARTSKIP_DEFERRED)
            except Exception:
                pass
        logger.error("SmartSkip session expired: %s", e)
        summary.duration_seconds = time.time() - started
        return summary
    except ss.SmartSkipPaymentError as e:
        summary.deferred = summary.submitted
        summary.deferral_reason = "insufficient account balance — top up needed"
        summary.submitted = 0
        for _, rec in rows_and_records:
            try:
                vt.mark_state(rec["uuid"], vt.RECORD_TAG_SMARTSKIP_DEFERRED)
            except Exception:
                pass
        logger.error("SmartSkip payment error: %s", e)
        summary.duration_seconds = time.time() - started
        return summary
    except ss.SmartSkipTimeoutError as e:
        summary.deferred = summary.submitted
        summary.deferral_reason = f"batch timeout ({e})"
        summary.submitted = 0
        for _, rec in rows_and_records:
            try:
                vt.mark_state(rec["uuid"], vt.RECORD_TAG_SMARTSKIP_DEFERRED)
            except Exception:
                pass
        logger.error("SmartSkip batch timeout: %s", e)
        summary.duration_seconds = time.time() - started
        return summary

    # ── 4. Parse results + apply to each record
    results = list(ss.parse_result_csv(result_csv_path))
    logger.info("Parsed %d results from SmartSkip", len(results))

    # Map row_index → parent record via the order we submitted
    for result in results:
        if result.row_index >= len(rows_and_records):
            logger.warning("Result row_index %d exceeds submitted batch size",
                           result.row_index)
            continue
        _, parent_record = rows_and_records[result.row_index]

        heirs, errors = _apply_smartskip_result(
            parent_record, result, scraped_date,
        )
        summary.heirs_created += heirs
        summary.heir_creation_errors += errors
        if result.has_data:
            summary.matched += 1
        else:
            summary.no_match += 1

    summary.duration_seconds = time.time() - started
    return summary


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rehash-only", action="store_true",
                   help="Only process records not yet tagged traced_smartskip")
    p.add_argument("--max-records", type=int,
                   help="Cap batch size (for testing)")
    p.add_argument("--dry-run", action="store_true",
                   help="Log what would be submitted, don't upload")
    p.add_argument("--headed", action="store_true",
                   help="Show the Playwright browser (headless=False)")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post SmartSkip summary to SLACK_WEBHOOK_URL when done")
    p.add_argument("--recover-batch", metavar="CSV_NAME",
                   help="Recover a paid batch that failed after submit. Pass the "
                        "CSV filename from outbox/smartskip/submitted/ (e.g. "
                        "'Probate_Cascade_2026-08-06_2026-08-06_091924.csv'). "
                        "Skips submit + payment; goes straight to poll + download.")
    p.add_argument("--merge-from-csv", metavar="RESULT_CSV",
                   help="Skip Playwright entirely — merge a result CSV that was "
                        "manually downloaded from SmartSkip. The submitted CSV "
                        "(same filename minus 'result_' prefix) must exist in "
                        "outbox/smartskip/submitted/ so we can reconstruct the "
                        "row-to-record mapping.")
    p.add_argument("--and-standard-cascade", action="store_true",
                   help="After SmartSkip merge, ALSO run the standard 3-vendor "
                        "cascade (Tracerfy → DataSift native → Enformion → "
                        "Trestle) on the same records. Emits ONE combined "
                        "Slack post covering both. Requires --notify-slack.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.merge_from_csv:
        # Merge-only mode — bypass all Playwright, just process a manually-
        # downloaded result CSV. Used when auto-download breaks but batch
        # completed on SmartSkip's side and operator downloads by hand.
        summary = _merge_manual_result(
            result_csv=Path(args.merge_from_csv),
            and_standard_cascade=args.and_standard_cascade,
        )
    elif args.recover_batch:
        # Recovery mode — batch was submitted + paid but Step 5 crashed,
        # so we lost the batch metadata. Skip submit, go straight to
        # polling Skips History for the CSV filename provided.
        summary = _recover_paid_batch(
            csv_name=args.recover_batch,
            headless=not args.headed,
        )
    else:
        summary = run_probate_cascade(
            rehash_only=args.rehash_only,
            max_records=args.max_records,
            dry_run=args.dry_run,
            headless=not args.headed,
        )

    print()
    print(summary.to_slack_block())
    print()

    # Persist summary for daily_finalize.py (or manual review)
    out = Path(__file__).parent.parent / "output" / "observability" / "smartskip_last_run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "submitted": summary.submitted,
        "matched": summary.matched,
        "no_match": summary.no_match,
        "heirs_created": summary.heirs_created,
        "heir_creation_errors": summary.heir_creation_errors,
        "deferred": summary.deferred,
        "deferral_reason": summary.deferral_reason,
        "duration_seconds": summary.duration_seconds,
        "cost_usd": summary.cost_usd,
        "dry_run": summary.dry_run,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))

    # Optional: post standalone Slack update. Used when this script runs
    # locally (independent of the daily-sweep GHA cron which can't run
    # Playwright/SmartSkip due to lack of persistent session cookies).
    if args.notify_slack:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
            from slack_notifier import _send_webhook
            block = summary.to_slack_block()
            _send_webhook(block)
            logger.info("SmartSkip summary posted to Slack")
        except Exception as e:
            logger.warning("Slack post failed: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(_main())
