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
    max_signers: int = 8,
) -> ResolverResult:
    """Run the address-anchored household waterfall for one NoticeData. Non-raising.

    Revised 2026-07-16 based on live testing (5/5 test cases): drops the
    old (decedent + N signer) waterfall in favor of a single household
    search by LastName + full address. Root cause of the revision:
    Enformion's ranking algorithm penalizes elderly recently-deceased
    people when a first name is included in the search (namesakes with
    fresher digital activity outrank our targets). Dropping first name
    and anchoring on street+city+state+zip returns the correct household
    every time.

    Flow:
      Step A. household_search(LastName + street + city + state + zip)
              → returns every person indexed at that address
      Step B. filter by age (>= 25 to be an adult heir; >= 55 to be
              likely spouse/decedent generation)
      Step C. classify each hit: DECEDENT / SPOUSE / ADULT_CHILD / OTHER
              (heuristic — see _classify_household_member)
      Step D. extract phones + addresses directly from Step A response
              (NO per-signer re-search needed — massive cost savings)
      Step E. optionally walk relativesSummary[] to find offsite heirs
              (obit-known survivors not living at the property) and
              search each with name+DOB → their phones

    Requires on the notice:
      * ``decedent_last_name``                       (household anchor)
      * ``address`` + ``city`` + ``state`` + ``zip`` (household anchor)

    Uses (optional):
      * ``decedent_first_name`` — for age-plausibility check on decedent match
      * ``date_of_death`` — for the DOD conflict flag (Enformion often lags)
      * ``all_survivors_list`` / heir_map_json — for offsite-heir search

    Cost per notice: ~1 API call (~$0.25 Starter) down from ~$2-3 in
    the old flow. Free trial covers ~100 records at this rate.
    """
    result = ResolverResult()

    if not enf.is_configured():
        result.disabled_reason = "not_configured"
        return result
    if enf.budget_remaining() <= 0:
        result.disabled_reason = "budget_exhausted"
        return result

    # Only LastName + address are strictly required in the new flow
    last = (notice.decedent_last_name or "").strip()
    first = (notice.decedent_first_name or "").strip()
    if not last:
        parts = (notice.decedent_name or "").split()
        if len(parts) >= 2:
            last = parts[-1]
            first = first or parts[0]
    if not last:
        result.disabled_reason = "missing_decedent_last_name"
        return result

    street = (notice.address or "").strip()
    city = (notice.city or "").strip()
    state = (notice.state or "AL").strip()
    zip_code = (notice.zip or "").strip()[:5]
    if not street or not zip_code:
        result.disabled_reason = "missing_property_address_or_zip"
        return result

    result.ran = True

    # ── Step A — household search ──
    logger.info(
        "Enformion Step A (household): LastName=%s @ %s, %s %s %s",
        last, street, city, state, zip_code,
    )
    response = enf.household_search(
        last_name=last, street=street, city=city, state=state,
        zip_code=zip_code, max_results=25,
    )
    result.searches_used += 1
    persons = (response.get("persons") or response.get("people")
               or response.get("results") or [])

    if not persons:
        logger.info("Enformion Step A: 0 persons at household")
        result.disabled_reason = "step_a_miss"
        return result

    logger.info("Enformion Step A: %d person(s) at household", len(persons))

    # ── Step B — classify household members ──
    # Enformion's death index lags for recently-deceased people; we can't
    # rely on datesOfDeath. Instead classify by age + name-match against
    # what we already know from the obit.
    classified = []
    for p in persons:
        role, confidence = _classify_household_member(p, notice, first)
        if role == "skip":
            continue
        classified.append((p, role, confidence))

    logger.info(
        "Enformion Step B: %d/%d household members classified as potential heirs",
        len(classified), len(persons),
    )

    # ── Step C — extract phones + addresses directly (no per-signer search) ──
    for p, role, confidence in classified[:max_signers]:
        name = enf.full_name(p)
        first_name = enf._get_first_name(p)
        last_name = enf._get_last_name(p)
        age = p.get("age")

        heir_phones = enf.phones(p)
        heir_addrs = enf.addresses(p)
        primary_addr = heir_addrs[0] if heir_addrs else ""

        heir = EnformionHeir(
            name=name, first=first_name, last=last_name,
            dob_year=str(age) if age else "",
            relationship=role,      # "decedent" | "spouse" | "adult_child" | "other_adult"
            verified_living=(role != "decedent"),
            verified_relationship=(confidence == "high"),
            signing_authority=(role in ("spouse", "adult_child")),
            address=primary_addr,
            phones=heir_phones,
            enformion_match=True,
        )
        result.signers.append(heir)
        logger.info(
            "  → %s (role=%s, age=%s, %d phone(s), confidence=%s)",
            name, role, age, len(heir_phones), confidence,
        )

    # Set decedent_matched based on whether we found ANY heir at the address
    # (household-hit = we know the family, even if the decedent record
    # itself isn't marked deceased in Enformion's stale death index).
    result.decedent_matched = True

    # ── DOD conflict — flag only if decedent record present + dates disagree ──
    decedent_hits = [p for p, r, _ in classified if r == "decedent"]
    if decedent_hits:
        enf_dod = enf.extract_dod(decedent_hits[0])
        result.dod = enf_dod
        obit_dod = (notice.date_of_death or "").strip()
        if enf_dod and obit_dod:
            enf_year = enf_dod[:4]
            obit_year = obit_dod[:4]
            if enf_year and obit_year and enf_year != obit_year:
                result.dod_conflict = True
                result.dod_conflict_note = (
                    f"Enformion death index says {enf_dod}, but obituary/notice "
                    f"says {obit_dod} — {abs(int(enf_year) - int(obit_year))}yr "
                    f"apart. The heir set stands, but the estate currently in "
                    f"probate may belong to a more recent household death. "
                    f"Verify with the closer."
                )
                logger.warning(
                    "Enformion DOD CONFLICT: enformion=%s obit=%s",
                    enf_dod, obit_dod,
                )

    # ── Step E (optional) — offsite heir search via relativesSummary ──
    # Adult children who moved out won't be in the household search. Walk
    # the decedent/spouse's relatives graph for closest-kin (level "ab")
    # people we DON'T already have from the household, and search them by
    # name+dob. Capped so we don't blow the budget.
    if decedent_hits or any(r == "spouse" for _, r, _ in classified):
        anchor = decedent_hits[0] if decedent_hits else next(
            p for p, r, _ in classified if r == "spouse"
        )
        result.all_relatives_summary = enf.relatives(anchor)
        offsite_added = _search_offsite_heirs(
            anchor_relatives=result.all_relatives_summary,
            already_have=[h.name for h in result.signers],
            decedent_surname=last,
            budget=max(0, max_signers - len(result.signers)),
        )
        for h in offsite_added:
            result.signers.append(h)
            result.searches_used += 1
        if offsite_added:
            logger.info(
                "Enformion Step E: +%d offsite heirs from relatives graph",
                len(offsite_added),
            )

    return result


# ── Classification + offsite heir helpers ────────────────────────────


def _classify_household_member(
    person: dict[str, Any],
    notice: "NoticeData",
    decedent_first: str,
) -> tuple[str, str]:
    """Assign a role to a household member. Returns (role, confidence).

    Roles (in order of downstream priority):
      - "decedent"    — likely the deceased owner (name+age match, elderly)
      - "spouse"      — likely surviving spouse (elderly, opposite/same surname)
      - "adult_child" — likely a child heir (age 25-55)
      - "other_adult" — age qualified but role unclear (still worth calling)
      - "skip"        — under 25, too young to be a signer

    Confidence: "high" when name+age match obit expectations; "medium" when
    inferred from age alone; "low" when only barely-plausible.
    """
    age = person.get("age") or 0
    first = enf._get_first_name(person).lower()
    decedent_first_lc = (decedent_first or "").lower()
    obit_dm_name = (notice.decision_maker_name or "").lower()

    # Age < 25: too young to be a signer (grandkid at same address)
    if age and age < 25:
        return "skip", "high"

    # Name matches obit's decedent first name → decedent (Enformion may not
    # know they died yet — classify by role, not by datesOfDeath)
    if decedent_first_lc and first and (
        first == decedent_first_lc
        or first.startswith(decedent_first_lc + " ")
        or decedent_first_lc.startswith(first + " ")
    ):
        conf = "high" if age >= 55 else "medium"
        return "decedent", conf

    # Name matches obit's decision-maker (usually spouse) → spouse
    if obit_dm_name and first and first in obit_dm_name.split():
        return "spouse", "high"

    # Age heuristic: 55+ → likely spouse-of-decedent generation
    if age >= 55:
        return "spouse", "medium"

    # 25-54 → likely adult child heir
    if age >= 25:
        return "adult_child", "medium"

    # No age data — treat as adult heir with low confidence
    return "other_adult", "low"


def _search_offsite_heirs(
    anchor_relatives: list[dict[str, Any]],
    already_have: list[str],
    decedent_surname: str,
    budget: int,
) -> list[EnformionHeir]:
    """Search offsite relatives (adult children living elsewhere) by name+DOB.

    Only searches level "ab" (closest kin) with a plausible DOB year and
    surname match (whole-token). Caps at ``budget`` searches to protect
    the API budget when a decedent has many relatives.
    """
    if budget <= 0:
        return []

    already_lc = {n.lower() for n in already_have}
    surname = (decedent_surname or "").lower()

    candidates = []
    for r in anchor_relatives:
        if r.get("deceased") or r.get("level") != enf.CLOSEST_KIN_LEVEL:
            continue
        if not r.get("dob"):
            continue
        # Must share the surname (whole-token match — protects against
        # "Maxwell" matching "Well" gotcha)
        rel_name = r.get("name", "")
        rel_lastname = r.get("lastname", "")
        if not enf.surname_matches(rel_name, rel_lastname, surname):
            continue
        if rel_name.lower() in already_lc:
            continue
        candidates.append(r)

    offsite_heirs: list[EnformionHeir] = []
    for r in candidates[:budget]:
        if enf.budget_remaining() <= 0:
            break
        name = r["name"]
        dob = r["dob"]
        parts = name.split()
        if len(parts) < 2:
            continue
        s_first, s_last = parts[0], parts[-1]
        s_resp = enf.person_search(s_first, s_last, dob_year=dob)
        s_person = enf.first_match(s_resp, prefer_first_name=s_first)
        if not s_person:
            continue
        addrs = enf.addresses(s_person)
        offsite_heirs.append(EnformionHeir(
            name=name, first=s_first, last=s_last, dob_year=dob,
            relationship="adult_child",  # closest-kin, likely child
            verified_living=True,
            verified_relationship=False,  # surname-only guess
            signing_authority=True,
            address=addrs[0] if addrs else "",
            phones=enf.phones(s_person),
            enformion_match=True,
        ))

    return offsite_heirs


# The pre-2026-07-16 flow used a `_derive_signers(rels, surname)` helper
# to filter relativesSummary[] to child-like closest-kin entries. Replaced
# by the household-anchored + age-classification approach above. The old
# strategy relied on Enformion's `relativeType` field having accurate
# "Son"/"Daughter" values, which live testing showed it usually doesn't
# — most entries came back as generic "Family".


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
