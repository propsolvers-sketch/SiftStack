"""Enformion heir-resolution waterfall — steps A → E on one NoticeData.

Composes ``enformion_client`` primitives into the Deep Prospecting v4 skill's
Primary Path:

    A. Person Search the deceased           -> relatives graph + DOD
    B. Derive REQUIRED SIGNERS              -> living closest-kin children
    C. Person Search each signer (name+DOB) -> address + phones per signer
    D. Dedupe phones across signers         -> unique dial candidates
    E. (deferred) Trestle score             -> handled by existing
       phone_validator.score_phones_for_pipeline in the calling adapter

Returns a structured result that pipelines can MERGE into the notice's
existing heir_map_json (from LLM extraction). Merge policy is caller-side —
this module only produces the enrichment.

Design notes:
  * "Fallback" mode (default in adapters): only run when the notice has
    zero DM #1 phones after Tracerfy. Guards against per-run spend on
    records that already have contact info.
  * Signer-gating (Step B) is the primary cost lever. We NEVER run
    per-signer Person Searches on non-signing kin (grandkids, cousins,
    in-laws) — they're skipped entirely per the skill.
  * A death-index vs obit-DOD conflict is SURFACED (not resolved) on the
    result — the calling adapter puts the flag into the DataSift Notes
    where the closer sees it before dialing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import enformion_client as enf

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)


@dataclass
class EnformionHeir:
    """One resolved signer with everything downstream needs."""
    name: str = ""
    first: str = ""
    last: str = ""
    dob_year: str = ""
    relationship: str = ""       # Enformion's relativeType (Son / Daughter / Spouse / ...)
    signing_authority: bool = True
    verified_living: bool = True  # True when Enformion returned a match on the per-signer search
    verified_relationship: bool = True  # False when we fell back to surname-only match
    address: str = ""            # most-recent full address string
    phones: list[dict[str, Any]] = field(default_factory=list)
    enformion_match: bool = False   # True when Step C returned a hit


@dataclass
class ResolverResult:
    """Everything the calling adapter needs to merge into the notice."""
    ran: bool = False              # True if the waterfall executed (creds present + budget OK)
    disabled_reason: str = ""      # "not_configured" / "budget_exhausted" / "step_a_miss" / ""
    decedent_matched: bool = False
    dod: str = ""                  # YYYY-MM-DD from Enformion (may be YYYY-01-01 for masked)
    dod_conflict: bool = False     # True when obit DOD and Enformion DOD disagree by year
    dod_conflict_note: str = ""    # Human-readable summary for Notes
    signers: list[EnformionHeir] = field(default_factory=list)
    all_relatives_summary: list[dict[str, Any]] = field(default_factory=list)
    searches_used: int = 0         # Person-Search calls consumed (for cost accounting)


def resolve_heirs(
    notice: "NoticeData",
    *,
    max_signers: int = 5,
) -> ResolverResult:
    """Run the A→E waterfall for one NoticeData. Non-raising.

    Requires on the notice:
      * ``decedent_first_name`` + ``decedent_last_name``  (identity)
      * ``address`` + ``city`` + ``state`` + ``zip``      (anchor for Step A)

    Uses ``date_of_death`` for the DOD conflict check when present.

    Returns a ResolverResult. When ``ran=False``, the caller should treat
    this as a no-op (creds missing or budget hit — check disabled_reason).
    """
    result = ResolverResult()

    if not enf.is_configured():
        result.disabled_reason = "not_configured"
        return result
    if enf.budget_remaining() <= 0:
        result.disabled_reason = "budget_exhausted"
        return result

    first = (notice.decedent_first_name or "").strip()
    last = (notice.decedent_last_name or "").strip()
    if not first or not last:
        # Fall back to splitting decedent_name if the pieces are missing
        parts = (notice.decedent_name or "").split()
        if len(parts) >= 2:
            first = first or parts[0]
            last = last or parts[-1]
    if not first or not last:
        result.disabled_reason = "missing_decedent_name"
        return result

    city = (notice.city or "").strip()
    state = (notice.state or "AL").strip()
    zip_code = (notice.zip or "").strip()[:5]
    if not zip_code:
        # Enformion rejects under-specified queries — a name+city hit is
        # rejected as "insufficient criteria". We need the anchor.
        result.disabled_reason = "missing_property_zip"
        return result

    result.ran = True

    # ── Step A — Person Search the decedent ──
    logger.info(
        "Enformion Step A: searching decedent %s %s @ %s, %s %s",
        first, last, city, state, zip_code,
    )
    response = enf.person_search(
        first, last, city=city, state=state, zip_code=zip_code,
    )
    result.searches_used += 1
    decedent = enf.first_match(response)
    if not decedent:
        logger.info("Enformion Step A: no match for %s %s", first, last)
        result.disabled_reason = "step_a_miss"
        return result

    result.decedent_matched = True
    result.dod = enf.extract_dod(decedent)
    rels = enf.relatives(decedent)
    result.all_relatives_summary = rels

    # DOD conflict check — surface, do not resolve
    obit_dod = (notice.date_of_death or "").strip()
    if obit_dod and result.dod:
        obit_year = obit_dod[:4]
        enf_year = result.dod[:4]
        if obit_year and enf_year and obit_year != enf_year:
            result.dod_conflict = True
            result.dod_conflict_note = (
                f"Enformion death index says {result.dod}, but obituary/notice "
                f"says {obit_dod} — {abs(int(enf_year) - int(obit_year))}yr "
                f"apart. The heir set below likely stands either way, but the "
                f"estate currently in probate may belong to a more recent "
                f"household death (e.g. surviving spouse). Surface with the "
                f"closer; verify before treating as legal fact."
            )
            logger.warning(
                "Enformion DOD CONFLICT: enformion=%s obit=%s (%s %s)",
                result.dod, obit_dod, first, last,
            )

    logger.info(
        "Enformion Step A hit: DOD=%s  relatives=%d (%d closest-kin)",
        result.dod or "unknown", len(rels),
        sum(1 for r in rels if r["level"] == enf.CLOSEST_KIN_LEVEL),
    )

    # ── Step B — derive required signers ──
    signers = _derive_signers(rels, last)
    logger.info(
        "Enformion Step B: %d required signers (cost gate — non-signers skipped)",
        len(signers),
    )
    if not signers:
        # All closest-kin children are deceased or none present. Result
        # still carries relatives_summary so the caller can flag per stirpes.
        return result

    # ── Step C — resolve each signer (name + DOB year) ──
    for signer_dict in signers[:max_signers]:
        if enf.budget_remaining() <= 0:
            logger.warning(
                "Enformion Step C: budget hit mid-waterfall — %d/%d signers resolved",
                len(result.signers), len(signers),
            )
            break

        name = signer_dict["name"]
        dob_year = signer_dict["dob"]
        parts = name.split()
        s_first = parts[0]
        s_last = parts[-1]
        logger.info(
            "Enformion Step C: resolving %s (dob %s)", name, dob_year,
        )
        s_resp = enf.person_search(s_first, s_last, dob_year=dob_year)
        result.searches_used += 1
        s_person = enf.first_match(s_resp)

        heir = EnformionHeir(
            name=name, first=s_first, last=s_last, dob_year=dob_year,
            relationship=signer_dict["type"] or "child",
            verified_relationship=signer_dict.get("verified", True),
            signing_authority=True,
        )

        if s_person:
            heir.enformion_match = True
            heir.verified_living = True
            addrs = enf.addresses(s_person)
            if addrs:
                heir.address = addrs[0]
            heir.phones = enf.phones(s_person)
            logger.info(
                "  → %s: %d phone(s), address=%s",
                name, len(heir.phones), heir.address or "(none)",
            )
        else:
            heir.enformion_match = False
            heir.verified_living = False
            logger.info("  → %s: no name+DOB match", name)

        result.signers.append(heir)

    return result


def _derive_signers(
    rels: list[dict[str, Any]], surname: str,
) -> list[dict[str, Any]]:
    """Step B — required signers = living closest-kin children.

    Prefer ``relativeType`` (Son / Daughter / Child) when present. When
    blank, fall back to a WHOLE-TOKEN surname match — but flag it as
    verified=False so the caller knows to confirm the relationship
    before treating as a required signer.

    Requires ``dob`` (year) — needed for Step C's name+DOB lookup.
    """
    out: list[dict[str, Any]] = []
    for r in rels:
        if r["deceased"]:
            continue
        if r["level"] != enf.CLOSEST_KIN_LEVEL:
            continue
        if not r["dob"]:
            continue
        rtype = (r["type"] or "").lower()
        is_labeled_child = rtype in ("son", "daughter", "child")
        if is_labeled_child:
            out.append({**r, "verified": True})
        elif not rtype and enf.surname_matches(r["name"], r["lastname"], surname):
            out.append({**r, "verified": False})   # surname-only guess
    return out


# ── Merge into NoticeData ────────────────────────────────────────────


def merge_into_notice(
    notice: "NoticeData",
    result: ResolverResult,
    *,
    existing_heirs: list[dict] | None = None,
) -> dict[str, Any]:
    """Merge resolver output back into the notice.

    Strategy:
      * Take the union of existing_heirs (from LLM obit extraction) and
        result.signers, keyed by normalized full name.
      * For each match, Enformion wins on address + phones + verification
        status (deterministic source of truth). LLM wins on relationship
        when Enformion's relativeType was blank.
      * New Enformion-only signers (not in LLM's list) get added — the
        obit may have missed them.

    Returns a stats dict for the caller's log (phones_added, verified_count,
    net_new_signers). Writes back to notice.heir_map_json + notice.primary_phone
    / mobile_1..5 / landline_1..3 as needed to promote Enformion phones
    into the DataSift CSV columns.
    """
    from notice_parser import NoticeData      # local import to avoid cycle
    import json as _json

    if existing_heirs is None:
        existing_heirs = []
        if notice.heir_map_json:
            try:
                existing_heirs = _json.loads(notice.heir_map_json) or []
            except (_json.JSONDecodeError, TypeError):
                existing_heirs = []

    def _key(name: str) -> str:
        return "".join(c for c in name.lower() if c.isalnum())

    by_key = {_key(h.get("name", "")): h for h in existing_heirs if h.get("name")}

    stats = {
        "phones_added": 0,
        "verified_count": 0,
        "net_new_signers": 0,
        "addresses_filled": 0,
    }

    for heir in result.signers:
        key = _key(heir.name)
        existing = by_key.get(key)

        if existing is None:
            # New signer Enformion found that LLM's obit extraction missed
            new_entry: dict[str, Any] = {
                "name": heir.name,
                "relationship": heir.relationship,
                "signing_authority": True,
                "status": ("verified_living" if heir.verified_living else "unverified"),
                "source": "enformion_person_search",
                "enformion_matched": heir.enformion_match,
                "phones": [p["number"] for p in heir.phones],
            }
            if heir.address:
                new_entry["mailing_address"] = heir.address
                # Try to break into street/city/state/zip for the Notes formatter
                # locale check. Best-effort parse of the fullAddress string.
                street, city, state, zip5 = _parse_full_address(heir.address)
                if street:
                    new_entry["street"] = street
                if city:
                    new_entry["city"] = city
                if state:
                    new_entry["state"] = state
                if zip5:
                    new_entry["zip"] = zip5
                stats["addresses_filled"] += 1
            by_key[key] = new_entry
            stats["net_new_signers"] += 1
            if heir.phones:
                stats["phones_added"] += len(heir.phones)
            if heir.verified_living:
                stats["verified_count"] += 1
        else:
            # Merge with existing LLM entry — Enformion wins on data,
            # LLM keeps its position + any pre-existing fields.
            if heir.verified_living:
                existing["status"] = "verified_living"
                existing["enformion_matched"] = True
                stats["verified_count"] += 1
            if heir.relationship and not existing.get("relationship"):
                existing["relationship"] = heir.relationship
            if heir.address and not existing.get("street"):
                street, city, state, zip5 = _parse_full_address(heir.address)
                if street:
                    existing["street"] = street
                if city:
                    existing["city"] = city
                if state:
                    existing["state"] = state
                if zip5:
                    existing["zip"] = zip5
                existing["mailing_address"] = heir.address
                stats["addresses_filled"] += 1
            # Merge phones (dedup by digits)
            existing_phones = set(existing.get("phones") or [])
            for p in heir.phones:
                if p["number"] not in existing_phones:
                    existing.setdefault("phones", []).append(p["number"])
                    existing_phones.add(p["number"])
                    stats["phones_added"] += 1

    merged = list(by_key.values())
    notice.heir_map_json = _json.dumps(merged, ensure_ascii=False)

    # Refresh signing_chain_count / signing_chain_names / heirs_verified_living
    signers = [h for h in merged if h.get("signing_authority")]
    notice.signing_chain_count = str(len(signers)) if signers else ""
    notice.signing_chain_names = ", ".join(
        h.get("name", "") for h in signers if h.get("name")
    )
    living = sum(1 for h in signers if h.get("status") == "verified_living")
    notice.heirs_verified_living = str(living) if living else ""

    # Promote DM #1's Enformion phones into the flat NoticeData slots so
    # the DataSift Phone 1-9 columns populate (existing formatter reads
    # from these — see _PHONE_FIELD_LABELS in datasift_formatter.py). Only
    # touch slots that are currently empty; don't clobber Tracerfy hits.
    _promote_enformion_phones_to_flat_slots(notice, result)

    return stats


def _parse_full_address(full: str) -> tuple[str, str, str, str]:
    """Best-effort split of a full-address string into (street, city, state, zip5).

    Enformion returns "123 Oak St, Knoxville, TN 37918" or similar. We
    handle the common comma-delimited shape and gracefully degrade to
    partial data when the format varies.
    """
    if not full:
        return "", "", "", ""
    parts = [p.strip() for p in full.split(",")]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    state = ""
    zip5 = ""
    if len(parts) > 2:
        tail = parts[-1].strip().split()
        if len(tail) >= 2 and len(tail[-1]) >= 5 and tail[-1][:5].isdigit():
            state = tail[0]
            zip5 = tail[-1][:5]
        elif len(tail) == 1 and len(tail[0]) == 2:
            state = tail[0]
    return street, city, state, zip5


_FLAT_PHONE_SLOTS = (
    "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
    "mobile_5", "landline_1", "landline_2", "landline_3",
)


def _promote_enformion_phones_to_flat_slots(
    notice: "NoticeData", result: ResolverResult,
) -> None:
    """Fill empty flat phone slots with Enformion phones (Mobile → mobile_N,
    Landline → landline_N, primary_phone left alone if already set).

    Same shape as _promote_heir_contacts_to_csv_slots but sourced from
    Enformion's per-phone metadata (which includes phoneType). Existing
    values are never overwritten — Tracerfy hits take precedence.
    """
    if not result.signers:
        return

    dm1_name = (notice.decision_maker_name or "").strip().lower()

    # Collect Enformion phones with their line type. Prefer DM #1's
    # phones first, then other signers, so DM #1 gets first pick of the
    # limited flat slots.
    ranked_phones: list[tuple[str, str]] = []
    dm1_heir = next((h for h in result.signers if h.name.lower() == dm1_name), None)
    if dm1_heir:
        ranked_phones.extend((p["number"], p["type"]) for p in dm1_heir.phones)
    for h in result.signers:
        if h is dm1_heir:
            continue
        ranked_phones.extend((p["number"], p["type"]) for p in h.phones)

    # Dedup preserving first-seen order
    seen = set()
    unique = []
    for num, ptype in ranked_phones:
        if num not in seen:
            seen.add(num)
            unique.append((num, ptype))

    # Skip any phone already present in a flat slot
    existing_flat = {
        (getattr(notice, slot, "") or "").strip()
        for slot in _FLAT_PHONE_SLOTS
    }
    unique = [(n, t) for n, t in unique if n not in existing_flat]

    if not unique:
        return

    mobile_slots = ["mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5"]
    landline_slots = ["landline_1", "landline_2", "landline_3"]
    misc_slots = list(mobile_slots) + list(landline_slots)

    # If primary_phone empty and we have anything, put the best there first
    if not (getattr(notice, "primary_phone", "") or "").strip() and unique:
        num, _ = unique.pop(0)
        setattr(notice, "primary_phone", num)

    for num, ptype in unique:
        pt = (ptype or "").lower()
        target_pool = mobile_slots if pt == "mobile" else (
            landline_slots if pt in ("landline", "fixed", "voip") else misc_slots
        )
        # Fill the first empty slot in the preferred pool
        placed = False
        for slot in target_pool:
            if not (getattr(notice, slot, "") or "").strip():
                setattr(notice, slot, num)
                placed = True
                break
        if not placed:
            # Preferred pool full — try any remaining flat slot
            for slot in _FLAT_PHONE_SLOTS:
                if not (getattr(notice, slot, "") or "").strip():
                    setattr(notice, slot, num)
                    break
