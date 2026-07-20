"""DataSift export → SiftStack enrichment → DataSift update-mode re-upload.

Watches ``~/Desktop/SiftStack/inbox/`` for DataSift CSV exports (Notice of
Default lists, courthouse-data lists post-skip-trace, or any other custom
list you drop in). For each new CSV:

  1. Pick the best skip-trace address per record (mailing-first with
     PO-Box detection + property fallback)
  2. Recover missing owner names via Enformion AddressID
  3. Run full enrichment (Tracerfy + Enformion household + Trestle) —
     OR Trestle-only if the record already has Trestle-tagged phones
     (round-trip case)
  4. Write an update CSV to ``~/Desktop/SiftStack/outbox/`` containing
     ONLY the phone/tag columns — Owner Name, Mailing, Property, Tags,
     Lists, Notice Type are all left untouched to protect our
     mortgagor/heir/decedent extractions

Ships as a bridge until DataSift API keys land (~90 days per DataSift's
estimate; may slip to 6mo). Once the API is available, the enrichment
core (Tracerfy + Enformion + Trestle) stays; only the file-based
in/out plumbing gets swapped for API calls.

Usage:
    # Overnight cron (processes anything in inbox/)
    python scripts/enrich_datasift_export.py

    # Explicit single-file (skips inbox scan)
    python scripts/enrich_datasift_export.py --input path/to/file.csv

    # Dry-run — log what would happen, don't spend API calls
    python scripts/enrich_datasift_export.py --dry-run

    # Verbose
    python scripts/enrich_datasift_export.py -v

Weekly rhythm for the operator:
    * Friday   — export from DataSift → drop CSVs in inbox/
    * Overnight — cron runs this script; enriched CSVs land in outbox/
    * Monday   — upload each outbox/ CSV to DataSift in Update Existing mode
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any

# Make src/ importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path.home() / "Desktop/SiftStack/.env")

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────

# Resolve paths from the script location so the tool works in GHA (where
# HOME is /home/runner, not /Users/shanismith) — falls back to
# ~/Desktop/SiftStack for local runs where the operator uses the Desktop
# working tree.
_REPO_ROOT = Path(__file__).parent.parent.resolve()
INBOX = _REPO_ROOT / "inbox"
OUTBOX = _REPO_ROOT / "outbox"
LOGS_DIR = _REPO_ROOT / "logs"
PROCESSED = INBOX / "processed"

# ── DataSift CSV column names (canonical — matches datasift_formatter) ──

COL_PROPERTY_STREET = "Property Street Address"
COL_PROPERTY_CITY = "Property City"
COL_PROPERTY_STATE = "Property State"
COL_PROPERTY_ZIP = "Property ZIP Code"
COL_OWNER_FIRST = "Owner First Name"
COL_OWNER_LAST = "Owner Last Name"
COL_MAILING_STREET = "Mailing Street Address"
COL_MAILING_CITY = "Mailing City"
COL_MAILING_STATE = "Mailing State"
COL_MAILING_ZIP = "Mailing ZIP Code"
COL_NOTES = "Notes"

PHONE_COLS = [f"Phone {i}" for i in range(1, 10)]
PHONE_TYPE_COLS = [f"Phone Type {i}" for i in range(1, 10)]
PHONE_STATUS_COLS = [f"Phone Status {i}" for i in range(1, 10)]
PHONE_TAG_COLS = [f"Phone Tags {i}" for i in range(1, 10)]

# ── PO Box detection ────────────────────────────────────────────────

_PO_BOX_RE = re.compile(
    r"\b(p\.?\s*o\.?\s*box|po\s*box|pobox|p\s+o\s+box)\b",
    re.IGNORECASE,
)


def _is_po_box(street: str) -> bool:
    return bool(_PO_BOX_RE.search(street or ""))


# ── Address normalization for equality comparison ───────────────────

_STREET_SUFFIX_MAP = {
    "ROAD": "RD", "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
    "PLACE": "PL", "TERRACE": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "SQUARE": "SQ", "TRAIL": "TRL",
}


def _normalize_address(street: str, zip_code: str = "") -> str:
    """Canonical form for equality comparison: uppercase, strip punctuation,
    canonicalize street suffixes, first 5 chars of ZIP."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (street or "").upper())
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    for i, p in enumerate(parts):
        if p in _STREET_SUFFIX_MAP:
            parts[i] = _STREET_SUFFIX_MAP[p]
    s = " ".join(parts)
    z = (zip_code or "").strip()[:5]
    return f"{s}|{z}" if z else s


# ── Skip-trace address selection ────────────────────────────────────


@dataclass
class SkipTraceTarget:
    """Which address to use for skip-trace + why. Included in per-record log."""
    street: str
    city: str
    state: str
    zip_code: str
    source: str    # "mailing" | "property" | "mailing_equals_property"
    reason: str    # human-readable audit line

    def is_valid(self) -> bool:
        return bool(self.street.strip() and self.zip_code.strip())


def choose_skip_trace_target(row: dict[str, str]) -> SkipTraceTarget:
    """Pick the address to anchor all skip-trace queries on.

    Rules (per operator spec 2026-07-18):
      1. Mailing present + real physical address + differs from property → mailing
      2. Mailing = property (owner-occupied) → property (same result either way)
      3. Mailing is PO Box → property (PO Boxes can't be reverse-looked-up)
      4. Mailing missing entirely → property
    """
    m_street = (row.get(COL_MAILING_STREET) or "").strip()
    m_city = (row.get(COL_MAILING_CITY) or "").strip()
    m_state = (row.get(COL_MAILING_STATE) or "").strip()
    m_zip = (row.get(COL_MAILING_ZIP) or "").strip()

    p_street = (row.get(COL_PROPERTY_STREET) or "").strip()
    p_city = (row.get(COL_PROPERTY_CITY) or "").strip()
    p_state = (row.get(COL_PROPERTY_STATE) or "").strip()
    p_zip = (row.get(COL_PROPERTY_ZIP) or "").strip()

    if not m_street:
        return SkipTraceTarget(
            street=p_street, city=p_city, state=p_state, zip_code=p_zip,
            source="property",
            reason="no mailing on file → property fallback",
        )

    if _is_po_box(m_street):
        return SkipTraceTarget(
            street=p_street, city=p_city, state=p_state, zip_code=p_zip,
            source="property",
            reason=f"mailing is PO Box ({m_street[:50]!r}), can't reverse-lookup → property fallback",
        )

    m_norm = _normalize_address(m_street, m_zip)
    p_norm = _normalize_address(p_street, p_zip)
    if m_norm == p_norm:
        return SkipTraceTarget(
            street=p_street, city=p_city, state=p_state, zip_code=p_zip,
            source="mailing_equals_property",
            reason="mailing = property (owner-occupied)",
        )

    return SkipTraceTarget(
        street=m_street, city=m_city, state=m_state, zip_code=m_zip,
        source="mailing",
        reason=f"absentee pattern — mailing {m_street} @ {m_zip} differs from property {p_street} @ {p_zip}",
    )


# ── Per-record enrichment ───────────────────────────────────────────


@dataclass
class RecordResult:
    """Outcome of enriching one row."""
    row_idx: int
    property_address: str
    action: str          # "full_enrichment" | "trestle_only" | "owner_recovery_full" | "skipped" | "noop"
    target: SkipTraceTarget | None = None
    owner_recovered: str = ""     # name added via AddressID (if any)
    phones_added: int = 0
    phones_scored: int = 0
    phones_corroborated: int = 0
    tier_distribution: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)   # source → phone count contributed
    updated_row: dict[str, str] = field(default_factory=dict)
    error: str = ""
    notes_appended: str = ""


def _has_trestle_tag(row: dict[str, str]) -> bool:
    """True when at least one Phone Tags column has a non-empty value —
    signals 'we've been here before' (round-trip case)."""
    for col in PHONE_TAG_COLS:
        if (row.get(col) or "").strip():
            return True
    return False


def _existing_phones(row: dict[str, str]) -> list[tuple[str, str]]:
    """Return [(phone, tag), ...] for phones already in the row.

    Phones from DataSift's built-in skip-trace show up in Phone 1-9
    without Phone Tags (untagged). Phones we uploaded from our own
    pipeline have both.
    """
    out = []
    for phone_col, tag_col in zip(PHONE_COLS, PHONE_TAG_COLS):
        phone = (row.get(phone_col) or "").strip()
        if phone:
            tag = (row.get(tag_col) or "").strip()
            out.append((phone, tag))
    return out


def _normalize_phone(p: str) -> str:
    digits = "".join(c for c in (p or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else ""


def enrich_row(
    row: dict[str, str], row_idx: int,
    *, dry_run: bool = False,
) -> RecordResult:
    """Apply the enrichment decision matrix to one CSV row.

    Non-raising. Errors are captured in result.error.
    """
    result = RecordResult(
        row_idx=row_idx,
        property_address=(row.get(COL_PROPERTY_STREET) or "").strip(),
        action="skipped",
    )

    target = choose_skip_trace_target(row)
    result.target = target

    owner_first = (row.get(COL_OWNER_FIRST) or "").strip()
    owner_last = (row.get(COL_OWNER_LAST) or "").strip()
    owner_present = bool(owner_first or owner_last)

    if not owner_present and not target.is_valid():
        result.action = "skipped"
        result.error = "no owner name AND no valid skip-trace address"
        return result

    existing = _existing_phones(row)
    has_tags = _has_trestle_tag(row)

    # ── Decision matrix ──
    if has_tags:
        # Round-trip case — we've enriched this before. Only Trestle-score
        # any untagged phones (new DataSift-added phones typically).
        untagged = [p for (p, t) in existing if not t]
        if not untagged:
            result.action = "noop"
            return result
        result.action = "trestle_only"
        if dry_run:
            return result
        _run_trestle_only(row, untagged, result)
        return result

    # Otherwise → full enrichment (may include owner recovery first)
    if not owner_present:
        result.action = "owner_recovery_full"
        if dry_run:
            return result
        recovered = _recover_owner_via_addressid(target, result)
        if recovered:
            owner_first, owner_last = recovered
            row[COL_OWNER_FIRST] = owner_first
            row[COL_OWNER_LAST] = owner_last
            result.owner_recovered = f"{owner_first} {owner_last}".strip()
            result.notes_appended += (
                f"\n⚠ Owner recovered via Enformion AddressID on "
                f"{target.source} address ({target.street}) — verify identity "
                f"before high-cost outreach; may be tenant on absentee-owned property.\n"
            )
        else:
            result.error = "owner_recovery_failed"
            result.action = "skipped"
            return result
    else:
        result.action = "full_enrichment"
        if dry_run:
            return result

    # Full enrichment: Tracerfy + Enformion household + Trestle
    _run_full_enrichment(row, target, owner_first, owner_last, existing, result)
    return result


# ── Enformion + Tracerfy + Trestle plumbing ────────────────────────


def _recover_owner_via_addressid(
    target: SkipTraceTarget, result: RecordResult,
) -> tuple[str, str] | None:
    """AddressID → best-effort owner recovery. Returns (first, last) or None."""
    try:
        import enformion_client as enf
        if not enf.is_configured():
            result.error = "AddressID skipped — Enformion not configured"
            return None
        resp = enf.address_id(
            street=target.street, city=target.city,
            state=target.state, zip_code=target.zip_code,
        )
        person = enf.address_id_person(resp)
        if not person:
            return None
        first = enf._get_first_name(person)
        last = enf._get_last_name(person)
        if first or last:
            logger.info(
                "  AddressID recovered owner: %s %s (source=%s address %s)",
                first, last, target.source, target.street,
            )
            return (first, last)
    except Exception as e:
        logger.warning("  AddressID recovery failed: %s", e)
        result.error = f"AddressID error: {e}"
    return None


def _run_trestle_only(
    row: dict[str, str], untagged_phones: list[str],
    result: RecordResult,
) -> None:
    """Score just the untagged phones. Preserve existing tagged ones."""
    try:
        from phone_validator import score_phones_for_pipeline
        import config as cfg
        api_key = getattr(cfg, "TRESTLE_API_KEY", "")
        if not api_key:
            logger.info("  Trestle-only skipped — TRESTLE_API_KEY not set")
            return
        from phone_validator import score_record_phones
        # Build a synthetic NoticeData for Trestle scoring
        from notice_parser import NoticeData
        n = NoticeData()
        # Load the untagged phones into flat slots
        for i, phone in enumerate(untagged_phones[:9]):
            if i == 0:
                n.primary_phone = phone
            elif i < 6:
                setattr(n, f"mobile_{i}", phone)
            else:
                setattr(n, f"landline_{i-5}", phone)
        tiers = score_record_phones([n], api_key=api_key, add_litigator=False)
        # Write back tier tags per phone
        for phone_col, tag_col in zip(PHONE_COLS, PHONE_TAG_COLS):
            phone = (row.get(phone_col) or "").strip()
            if not phone:
                continue
            existing_tag = (row.get(tag_col) or "").strip()
            if existing_tag:
                continue  # already tagged, don't overwrite
            # Look up tier — normalize both keys for match
            phone_key = _normalize_phone(phone)
            for k, v in tiers.items():
                if _normalize_phone(k) == phone_key:
                    tag = (v or {}).get("assigned_tag", "")
                    if tag:
                        row[tag_col] = tag
                        result.phones_scored += 1
                        result.tier_distribution[tag] = (
                            result.tier_distribution.get(tag, 0) + 1
                        )
                    break
        result.updated_row = row
        result.sources["datasift"] = len(untagged_phones)
    except Exception as e:
        logger.exception("Trestle-only enrichment failed")
        result.error = f"trestle_only_error: {e}"


def _run_full_enrichment(
    row: dict[str, str], target: SkipTraceTarget,
    owner_first: str, owner_last: str,
    existing: list[tuple[str, str]],
    result: RecordResult,
) -> None:
    """Full waterfall: Tracerfy → Enformion household → merge → Trestle → write back."""
    try:
        from notice_parser import NoticeData
        from phone_validator import score_record_phones
        import config as cfg
        import enformion_client as enf
        import enformion_heir_resolver as ehr

        # Build a synthetic NoticeData with what the CSV gave us + the
        # chosen skip-trace address as the anchor
        n = NoticeData()
        n.owner_name = f"{owner_first} {owner_last}".strip()
        # Sniff owner-last so Enformion household_search has a surname anchor
        n.decedent_last_name = owner_last or owner_first  # reuse the field
        n.address = target.street
        n.city = target.city
        n.state = target.state
        n.zip = target.zip_code
        n.county = ""  # unknown from generic DataSift export
        n.received_date = date.today().isoformat()
        n.date_added = date.today().isoformat()

        # Seed with pre-existing phones so we correctly track "datasift" as
        # a source for phones that were already on the record when we got it
        pre_existing_by_key: set[str] = set()
        for phone, _ in existing:
            key = _normalize_phone(phone)
            if key:
                pre_existing_by_key.add(key)

        # ── Tracerfy skip-trace ──
        tracerfy_added: set[str] = set()
        tracerfy_key = getattr(cfg, "TRACERFY_API_KEY", "")
        if tracerfy_key and n.owner_name and target.is_valid():
            try:
                import tracerfy_skip_tracer
                stats = tracerfy_skip_tracer.batch_skip_trace([n])
                logger.info(
                    "  Tracerfy: submitted=%d matched=%d phones=%d",
                    stats.get("submitted", 0),
                    stats.get("matched", 0),
                    stats.get("phones_found", 0),
                )
                # Any phones now on the notice that weren't in existing = Tracerfy's
                for attr in ("primary_phone", "mobile_1", "mobile_2", "mobile_3",
                             "mobile_4", "mobile_5", "landline_1", "landline_2",
                             "landline_3"):
                    ph = (getattr(n, attr, "") or "").strip()
                    if ph:
                        k = _normalize_phone(ph)
                        if k and k not in pre_existing_by_key:
                            tracerfy_added.add(k)
            except Exception as e:
                logger.warning("  Tracerfy skip-trace failed: %s", e)

        # ── Enformion household search ──
        enformion_added: set[str] = set()
        if enf.is_configured() and target.is_valid():
            try:
                result_ehr = ehr.resolve_heirs(n)
                if result_ehr.ran and result_ehr.signers:
                    ehr.merge_into_notice(n, result_ehr)
                    for signer in result_ehr.signers:
                        for p in signer.phones:
                            k = _normalize_phone(p["number"])
                            if k and k not in pre_existing_by_key and k not in tracerfy_added:
                                enformion_added.add(k)
                    logger.info(
                        "  Enformion: %d signers, %d searches used",
                        len(result_ehr.signers), result_ehr.searches_used,
                    )
            except Exception as e:
                logger.warning("  Enformion household failed: %s", e)

        # ── Merge: promote any newly-discovered phones into the flat slots
        # (respecting existing slot contents — don't clobber DataSift phones)
        _merge_phones_into_row(row, n, existing, result,
                                pre_existing=pre_existing_by_key,
                                tracerfy_added=tracerfy_added,
                                enformion_added=enformion_added)

        # ── Trestle score every unique phone now on the row ──
        _score_and_tag_all(row, cfg, result)

        result.updated_row = row

        # Source counts
        result.sources["datasift"] = len(pre_existing_by_key)
        result.sources["tracerfy"] = len(tracerfy_added)
        result.sources["enformion"] = len(enformion_added)

    except Exception as e:
        logger.exception("Full enrichment failed")
        result.error = f"full_enrichment_error: {e}"


def _merge_phones_into_row(
    row: dict[str, str], notice_data: Any, existing: list[tuple[str, str]],
    result: RecordResult, *, pre_existing: set, tracerfy_added: set,
    enformion_added: set,
) -> None:
    """Merge newly-discovered phones (from notice.primary_phone / mobile_N /
    landline_N / heir_map_json) into the row's Phone 1-9 slots. Never
    overwrites an existing phone — only fills empty slots."""

    # Collect all phones we now know about, in priority order
    new_phones: list[tuple[str, str]] = []  # (phone, line_type)

    for attr, ltype in [
        ("primary_phone", "Mobile"),
        ("mobile_1", "Mobile"), ("mobile_2", "Mobile"),
        ("mobile_3", "Mobile"), ("mobile_4", "Mobile"), ("mobile_5", "Mobile"),
        ("landline_1", "Landline"), ("landline_2", "Landline"),
        ("landline_3", "Landline"),
    ]:
        ph = (getattr(notice_data, attr, "") or "").strip()
        if ph:
            k = _normalize_phone(ph)
            if k and k not in pre_existing:
                new_phones.append((ph, ltype))

    # Also pull from heir_map_json phones (Enformion-found heir phones)
    heir_json = getattr(notice_data, "heir_map_json", "")
    if heir_json:
        try:
            heirs = json.loads(heir_json)
            for h in heirs:
                for p in h.get("phones") or []:
                    ph = p if isinstance(p, str) else (
                        p.get("number") or p.get("phone_number") or ""
                    )
                    if ph:
                        k = _normalize_phone(ph)
                        if k and k not in pre_existing:
                            new_phones.append((ph, ""))  # line type unknown
        except (json.JSONDecodeError, TypeError):
            pass

    # Dedup new_phones
    seen = set()
    unique_new = []
    for ph, lt in new_phones:
        k = _normalize_phone(ph)
        if k and k not in seen:
            seen.add(k)
            unique_new.append((ph, lt))

    # Fill empty Phone N slots with unique_new
    for ph, lt in unique_new:
        # Find first empty phone slot
        for phone_col, type_col in zip(PHONE_COLS, PHONE_TYPE_COLS):
            if not (row.get(phone_col) or "").strip():
                row[phone_col] = ph
                if lt and not (row.get(type_col) or "").strip():
                    row[type_col] = lt
                result.phones_added += 1
                break
        else:
            # No empty slots left — record has 9 phones already, drop the rest
            break


def _score_and_tag_all(row: dict[str, str], cfg, result: RecordResult) -> None:
    """Trestle-score every phone in Phone 1-9, write tier to Phone Tags N."""
    api_key = getattr(cfg, "TRESTLE_API_KEY", "")
    if not api_key:
        return
    try:
        from phone_validator import score_record_phones
        from notice_parser import NoticeData

        # Load ALL phones from row into a synthetic NoticeData
        n = NoticeData()
        for i, phone_col in enumerate(PHONE_COLS):
            ph = (row.get(phone_col) or "").strip()
            if not ph:
                continue
            if i == 0:
                n.primary_phone = ph
            elif i < 6:
                setattr(n, f"mobile_{i}", ph)
            else:
                setattr(n, f"landline_{i-5}", ph)

        tiers = score_record_phones([n], api_key=api_key, add_litigator=False)
        # Tag each phone
        for phone_col, tag_col in zip(PHONE_COLS, PHONE_TAG_COLS):
            phone = (row.get(phone_col) or "").strip()
            if not phone:
                continue
            existing_tag = (row.get(tag_col) or "").strip()
            if existing_tag:
                continue
            phone_key = _normalize_phone(phone)
            for k, v in tiers.items():
                if _normalize_phone(k) == phone_key:
                    tag = (v or {}).get("assigned_tag", "")
                    if tag:
                        row[tag_col] = tag
                        result.phones_scored += 1
                        result.tier_distribution[tag] = (
                            result.tier_distribution.get(tag, 0) + 1
                        )
                    break
    except Exception as e:
        logger.warning("Trestle scoring pass failed: %s", e)


# ── CSV I/O ─────────────────────────────────────────────────────────


# Columns the enrichment tool is ALLOWED to modify. Everything else
# passes through untouched. Protects Owner/Mailing/Property/Tags/Lists
# from the enrichment tool overwriting them.
_WRITABLE_COLS = set(PHONE_COLS + PHONE_TYPE_COLS + PHONE_STATUS_COLS + PHONE_TAG_COLS + [COL_NOTES])


def process_csv(input_path: Path, output_path: Path, *, dry_run: bool = False) -> dict:
    """Process one CSV. Returns per-file stats dict."""
    logger.info("═" * 72)
    logger.info("Processing: %s", input_path.name)
    logger.info("═" * 72)

    with input_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)

    stats = {
        "input": str(input_path),
        "records_total": len(rows),
        "records_full_enrichment": 0,
        "records_trestle_only": 0,
        "records_owner_recovered": 0,
        "records_skipped": 0,
        "records_noop": 0,
        "records_error": 0,
        "phones_added": 0,
        "phones_scored": 0,
        "tier_distribution": {},
        "address_source_counts": {"mailing": 0, "property": 0, "mailing_equals_property": 0},
        "source_contribution": {"datasift": 0, "tracerfy": 0, "enformion": 0},
        "dry_run": dry_run,
    }

    results: list[RecordResult] = []
    for idx, row in enumerate(rows):
        result = enrich_row(row, idx, dry_run=dry_run)
        results.append(result)

        # Accumulate stats
        if result.action == "full_enrichment":
            stats["records_full_enrichment"] += 1
        elif result.action == "trestle_only":
            stats["records_trestle_only"] += 1
        elif result.action == "owner_recovery_full":
            stats["records_owner_recovered"] += 1
            stats["records_full_enrichment"] += 1
        elif result.action == "skipped":
            stats["records_skipped"] += 1
        elif result.action == "noop":
            stats["records_noop"] += 1
        if result.error:
            stats["records_error"] += 1

        stats["phones_added"] += result.phones_added
        stats["phones_scored"] += result.phones_scored
        for tier, ct in result.tier_distribution.items():
            stats["tier_distribution"][tier] = (
                stats["tier_distribution"].get(tier, 0) + ct
            )
        if result.target:
            stats["address_source_counts"][result.target.source] = (
                stats["address_source_counts"].get(result.target.source, 0) + 1
            )
        for src, ct in result.sources.items():
            stats["source_contribution"][src] = (
                stats["source_contribution"].get(src, 0) + ct
            )

        logger.info(
            "  [%3d] %s @ %s — action=%s addr=%s phones+=%d scored=%d %s",
            idx + 1,
            (row.get(COL_OWNER_FIRST) or "?")[:12],
            result.property_address[:35],
            result.action,
            (result.target.source if result.target else "?"),
            result.phones_added,
            result.phones_scored,
            f"ERROR:{result.error}" if result.error else "",
        )

    if dry_run:
        logger.info("[DRY-RUN] Not writing output CSV — %s", output_path.name)
        return stats

    # Write update-mode CSV (identity columns + writable columns only)
    identity_cols = [
        COL_PROPERTY_STREET, COL_PROPERTY_CITY, COL_PROPERTY_STATE, COL_PROPERTY_ZIP,
        COL_OWNER_FIRST, COL_OWNER_LAST,
    ]
    output_cols = identity_cols + [c for c in header
                                    if c in _WRITABLE_COLS and c not in identity_cols]

    OUTBOX.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=output_cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            base = rows[r.row_idx]
            if r.updated_row:
                base.update(r.updated_row)
            # Append per-record enrichment note if any
            if r.notes_appended:
                existing_notes = base.get(COL_NOTES, "")
                base[COL_NOTES] = (existing_notes or "") + r.notes_appended
            w.writerow({c: base.get(c, "") for c in output_cols})

    logger.info("Wrote update CSV: %s", output_path)
    return stats


def _log_summary(stats: dict) -> None:
    logger.info("═" * 72)
    logger.info("ENRICHMENT SUMMARY")
    logger.info("═" * 72)
    logger.info("Records total:          %d", stats["records_total"])
    logger.info("  · Full enrichment:    %d", stats["records_full_enrichment"])
    logger.info("  · Trestle-only:       %d", stats["records_trestle_only"])
    logger.info("  · Owner recovered:    %d", stats["records_owner_recovered"])
    logger.info("  · No-op (already tagged): %d", stats["records_noop"])
    logger.info("  · Skipped:            %d", stats["records_skipped"])
    logger.info("  · Errors:             %d", stats["records_error"])
    logger.info("")
    logger.info("Address selection:")
    for src, ct in sorted(stats["address_source_counts"].items(), key=lambda x: -x[1]):
        logger.info("  · %-25s %d", src, ct)
    logger.info("")
    logger.info("Phones added:  %d", stats["phones_added"])
    logger.info("Phones scored: %d", stats["phones_scored"])
    logger.info("Tier distribution:")
    for tier, ct in sorted(stats["tier_distribution"].items(),
                            key=lambda x: (x[0] not in (
                                "Dial First", "Dial Second", "Dial Third",
                                "Dial Fourth", "Drop"), x[0])):
        logger.info("  · %-15s %d", tier, ct)
    logger.info("")
    logger.info("Source contribution (new phones added by vendor):")
    for src, ct in stats["source_contribution"].items():
        logger.info("  · %-15s %d", src, ct)
    logger.info("═" * 72)


# ── CLI ─────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Enrich DataSift CSV exports with Tracerfy + Enformion + Trestle. "
                    "Reads inbox/, writes outbox/. Drop DataSift exports in inbox/ "
                    "then re-upload the enriched output CSVs in Update Existing mode."
    )
    ap.add_argument("--input", type=Path, default=None,
                    help="Explicit CSV path (skips inbox scan)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log what would happen without spending API calls")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"enrich_datasift_{ts}.log"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_file)],
    )

    if args.input:
        inputs = [args.input]
    else:
        INBOX.mkdir(parents=True, exist_ok=True)
        inputs = sorted(p for p in INBOX.glob("*.csv") if p.is_file())
        if not inputs:
            logger.info(
                "No CSVs in %s. Drop DataSift export CSVs here to enrich them.",
                INBOX,
            )
            return 0

    all_stats = []
    for input_path in inputs:
        stem = input_path.stem
        suffix = "_ENRICHED" if not args.dry_run else "_DRYRUN"
        output_path = OUTBOX / f"{stem}{suffix}.csv"
        stats = process_csv(input_path, output_path, dry_run=args.dry_run)
        _log_summary(stats)
        all_stats.append(stats)

        # Move successfully-processed input to inbox/processed/ so the next
        # cron run doesn't re-enrich it. Dry-runs leave input in place —
        # they're diagnostic only. Explicit --input runs also skip the
        # move (operator drove that path manually).
        if not args.dry_run and not args.input:
            PROCESSED.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = PROCESSED / f"{input_path.stem}_{ts}{input_path.suffix}"
            shutil.move(str(input_path), str(dest))
            logger.info("Moved processed input to %s", dest)

    logger.info("")
    logger.info("Log written to %s", log_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
