"""Format NoticeData records into DataSift.ai (REISift) upload-ready CSV.

DataSift has 60+ built-in fields that auto-map when CSV headers match exactly.
This module maps our enrichment data to those built-in fields, plus 23 custom
fields in the "SiftStack" custom group for deep prospecting/notice-specific data.

For deceased records, the contact (Owner First/Last + Mailing Address) is set
to the decision maker, not the deceased owner. For living records, the contact
is the property owner.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR, LEADS_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Column order: auto-mapped built-in fields first, then custom fields.
# Headers must match DataSift's exact names for auto-mapping during upload.
DATASIFT_COLUMNS = [
    # ── Core (auto-mapped) ──
    "Property Street Address",
    "Property City",
    "Property State",
    "Property ZIP Code",
    "Owner First Name",
    "Owner Last Name",
    "Mailing Street Address",
    "Mailing City",
    "Mailing State",
    "Mailing ZIP Code",
    # ── Phone/Email (Tracerfy skip trace, mapped to DataSift built-in) ──
    # Per-phone 4-column block (Phone / Type / Status / Tags) matches the
    # DataSift upload template layout exactly so column auto-mapping works
    # without the Step-5 drag dance. Phone Tags N carries the Trestle tier
    # (Dial First / Dial Second / etc.) for the SPECIFIC phone in that slot
    # so the cold-caller can see dial order in DataSift directly without
    # cross-referencing Notes.
    "Phone 1", "Phone Type 1", "Phone Status 1", "Phone Tags 1",
    "Phone 2", "Phone Type 2", "Phone Status 2", "Phone Tags 2",
    "Phone 3", "Phone Type 3", "Phone Status 3", "Phone Tags 3",
    "Phone 4", "Phone Type 4", "Phone Status 4", "Phone Tags 4",
    "Phone 5", "Phone Type 5", "Phone Status 5", "Phone Tags 5",
    "Phone 6", "Phone Type 6", "Phone Status 6", "Phone Tags 6",
    "Phone 7", "Phone Type 7", "Phone Status 7", "Phone Tags 7",
    "Phone 8", "Phone Type 8", "Phone Status 8", "Phone Tags 8",
    "Phone 9", "Phone Type 9", "Phone Status 9", "Phone Tags 9",
    "Email 1",
    "Email 2",
    "Email 3",
    "Email 4",
    "Email 5",
    "Tags",
    "Lists",
    "Notes",
    # ── Built-in fields (auto-mapped by DataSift) ──
    "Estimated Value",
    "MSL Status",               # DataSift spells it "MSL" not "MLS"
    "Last Sale Date",
    "Last Sale Price",
    "Equity Percentage",
    "Tax Deliquent Value",      # DataSift typo — "Deliquent" not "Delinquent"
    "Tax Delinquent Year",
    "Tax Auction Date",
    "Foreclosure Date",
    "Probate Open Date",
    "Personal Representative",
    "Parcel ID",
    "Structure Type",
    "Year Built",
    "Living SqFt",
    "Bedrooms",
    "Bathrooms",
    "Lot (Acres)",
    # ── Custom fields (SiftStack group) ──
    "Notice Type",
    "County",
    "Date Added",
    "Owner Deceased",
    "Date of Death",
    "Decedent Name",
    "Decision Maker",
    "DM Relationship",
    "DM Confidence",
    "DM 2 Name",
    "DM 2 Relationship",
    "DM 3 Name",
    "DM 3 Relationship",
    "Obituary URL",
    "Source URL",
    # ── Deep prospecting fields ──
    "DM 1 Status",
    "DM 1 Source",
    "DM 2 Status",
    "DM 3 Status",
    "Heir Count",
    "Heirs Living",
    "Signing Chain Count",
    "Signing Chain Names",
    "DM Confidence Reason",
    "Data Flags",
    # ── Entity research fields ──
    "Entity Type",
    "Entity Contact",
    "Entity Contact Role",
    # ── AL probate enrichment (added 2026-04) ──
    # All become custom fields in the SiftStack group on first upload.
    "Probate Case Number",
    "Judge of Probate",
    "Probate Subtype",          # probate_creditors | probate_sale | probate_heirs_notice
    "Petition Filed Date",      # probate_sale only — when PR petitioned the court
    "Hearing Date",             # probate_sale only — court approval date
    "Creditor Claim Deadline",  # granted_date + 6 months (AL § 43-2-350)
    "Total Estate Value",       # Sum of all parcels owned by the decedent
]


def _format_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to M/D/YYYY."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return iso_date


def _heir_count(notice: NoticeData) -> str:
    """Return total heir count from heir_map_json, or empty string."""
    if not notice.heir_map_json:
        return ""
    try:
        return str(len(json.loads(notice.heir_map_json)))
    except (json.JSONDecodeError, TypeError):
        return ""


# Entity suffixes that indicate a business, not a person.
# DataSift marks records incomplete if owner name contains these without a real person.
_ENTITY_SUFFIXES = re.compile(
    r"\b(?:LLC|L\.L\.C|Corp|Corporation|Inc|Incorporated|Trust|LP|LLP|"
    r"LTD|Limited|Co\b|Company|Association|Partners|Partnership|Holdings)\b",
    re.IGNORECASE,
)


def _is_entity_name(name: str) -> bool:
    """Return True if name looks like a business entity, not a person."""
    return bool(_ENTITY_SUFFIXES.search(name))


# Leading prefix patterns that bleed in from upstream estate / heir parsing.
# "Estate of NAME" / "Heirs of NAME" / "the Estate of NAME" / orphaned "of NAME"
# all strip down to just "NAME" before the first/last split. Anchored at start
# so we don't eat legitimate name parts mid-string.
_ESTATE_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:the\s+)?(?:estate|heirs?|trust)\s+of\s+(?:the\s+(?:estate|trust)\s+of\s+)?"
    r"|of\s+"
    r"|in\s+re\s+(?:the\s+(?:estate|matter)\s+of\s+)?"
    r")",
    re.IGNORECASE,
)

# Role-suffix legalese from foreclosure / probate notices. Stripped from the
# first comma onwards once the comma is followed by a role keyword. Catches:
#   "GILCHRIST, SINGLE MAN"
#   "Powell, Mortgagor"
#   "SIMMONS, AN UNMARRIED PERSON"
#   "JONES, husband and wife"
#   "SMITH, a widow"
#   "DOE, as Trustee"
# Anything after the matched comma is dropped.
_ROLE_SUFFIX_RE = re.compile(
    r",\s*(?:a\s+|an\s+|as\s+|the\s+)?"
    r"(?:single|married|unmarried|surviving|widow(?:er)?|deceased|"
    r"mortgagor|borrower|spouse|trustee|grantor|grantee|tenant|"
    r"petitioner|respondent|husband|wife|"
    r"executor|executrix|administrator|administratrix|"
    r"personal\s+representative|attorney(?:-in-fact)?|"
    r"individually|jointly|as\s+joint|in\s+his|in\s+her|in\s+their)"
    r"(?:.*)$",  # everything from this point to end of string
    re.IGNORECASE,
)

# Bare role suffix without comma — e.g. "JONES SINGLE MAN" or "DOE WIDOW".
# Rare but seen. Matches the role keyword as a trailing whole-word and drops it.
_BARE_ROLE_SUFFIX_RE = re.compile(
    r"\s+(?:a\s+|an\s+)?"
    r"(?:single\s+(?:man|woman|person)|"
    r"unmarried\s+(?:man|woman|person)|"
    r"married\s+(?:man|woman|person|couple)|"
    r"husband\s+and\s+wife|wife\s+and\s+husband|widow(?:er)?)$",
    re.IGNORECASE,
)


def _clean_and_split_name(full_name: str) -> tuple[str, str]:
    """Clean a full name for DataSift upload and split into (first, last).

    Handles patterns that cause DataSift "incomplete" records:
    - Joint names with "&" or "AND": "John & Jane Smith" → ("John", "Smith")
    - Entity names (LLC, Trust, etc.): returns ("", "") — entity goes to Notes
    - Special characters: strips &, @, #, % from name parts
    - Role suffixes from foreclosure/probate legalese:
      ", a single man" / ", an unmarried person" / ", mortgagor" /
      ", husband and wife" / ", a widow" / etc. — stripped before splitting
    - Prefixes that bleed in from estate/heir parsing:
      "Estate of ..." / "of ..." / "Heirs of ..." — stripped so the actual
      person name is what gets split
    """
    if not full_name:
        return ("", "")

    name = full_name.strip()

    # Strip estate / heir / "of"-leading prefixes that escaped upstream cleanup.
    # Repeats once to catch chained prefixes like "Heirs of the Estate of NAME".
    for _ in range(2):
        name = _ESTATE_PREFIX_RE.sub("", name).strip()

    # Strip trailing role-suffix legalese (everything from `,` onwards once the
    # comma is followed by a role keyword). Catches "GILCHRIST, SINGLE MAN",
    # "Powell, Mortgagor", "SIMMONS, AN UNMARRIED PERSON", etc.
    name = _ROLE_SUFFIX_RE.sub("", name).strip()
    # Also handle role suffixes not preceded by a comma (rare but seen)
    name = _BARE_ROLE_SUFFIX_RE.sub("", name).strip()

    # Entity names → empty (don't put business names in person fields)
    if _is_entity_name(name):
        return ("", "")

    # Split joint owners on " & " or " AND " — keep first person only
    # "John & Jane Smith" → "John Smith"
    # "John David & Jane Marie Smith" → "John David Smith"
    joint_match = re.split(r"\s+(?:&|AND)\s+", name, maxsplit=1, flags=re.IGNORECASE)
    if len(joint_match) > 1:
        first_person = joint_match[0].strip()
        second_part = joint_match[1].strip()
        # Extract last name from second part (last word(s) after second person's first name)
        second_words = second_part.split()
        if len(second_words) >= 2:
            # "Jane Smith" → last name is "Smith"
            last_name = second_words[-1]
            # Check if first person already has a last name
            first_words = first_person.split()
            if len(first_words) == 1:
                # "John" & "Jane Smith" → "John Smith"
                name = f"{first_person} {last_name}"
            else:
                # "John David" & "Jane Marie Smith" → "John David Smith"
                # But if "John Smith" & "Jane Doe" → keep "John Smith"
                name = first_person
        else:
            # "John & Jane" with no last name → just use first person
            name = first_person

    # Strip remaining special characters that cause incomplete status
    name = re.sub(r"[&@#%]", "", name)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return ("", "")

    parts = name.split()
    # Strip trailing commas from each token. Catches "JANE," → "JANE" left
    # behind when a name like "JANE, EXECUTOR" got partially cleaned by
    # _ROLE_SUFFIX_RE (matched 'EXECUTOR' but the comma between Jane and
    # the role word survived). Per operator note: trailing-comma only —
    # no spaces/semicolons/etc.
    parts = [p.rstrip(",") for p in parts if p.rstrip(",")]
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    if len(parts) >= 3:
        # Strip middle initials (single letter + optional period) from between
        # first and last name parts. "Eric J. Yopp" → "Eric Yopp"
        # Keeps multi-char prefixes like "St." in "Richard C. St. Leger"
        middle = parts[1:-1]
        middle = [p for p in middle if not re.match(r"^[A-Za-z]\.?$", p)]
        parts = [parts[0]] + middle + [parts[-1]]
    return (parts[0], " ".join(parts[1:]))


def _split_name(full_name: str) -> tuple[str, str]:
    """Split full name into (first, last). Alias for _clean_and_split_name."""
    return _clean_and_split_name(full_name)


# Map notice_type → DataSift list name for niche sequential marketing.
# BOTH the DMs CSV and the Heirs CSV use this mapping for the Lists column —
# heirs land in the SAME DataSift list as the DM (Foreclosure, Probate, etc.)
# and are distinguished only by the per-row ``heir_of_<notice_type>`` tag.
# This avoids needing separate "Heirs of X" lists in DataSift.
NOTICE_TYPE_TO_LIST = {
    "foreclosure": "Foreclosure",
    "probate": "Probate",
    "pre_probate": "Pre-Probate",
    "tax_sale": "Tax Delinquent",
    "tax_delinquent": "Tax Delinquent",
    "eviction": "Eviction",
    "code_violation": "Code Violation",
    "divorce": "Divorce",
}

# Upload priority — lower numbers upload FIRST.
# Operator request 2026-06-11: route uploads code_violation → foreclosure →
# pre_probate → probate so that probate runs LAST and its swap_owners=ON
# enrich pass doesn't risk corrupting earlier (non-probate) uploads of the
# same property. Probate is rightful-last because the executor (PR) has
# actually filed with the courts to represent the estate, so they should
# overwrite any stale owner data from prior distressors at the same
# property address.
DISTRESSOR_PRIORITY: dict[str, int] = {
    "code_violation": 1,
    "foreclosure": 2,
    "tax_delinquent": 3,
    "tax_sale": 3,
    "eviction": 4,
    "divorce": 5,
    "pre_probate": 6,
    "probate": 7,  # LAST — see swap_owners reasoning above
}

# Which distressors trigger DataSift's "Swap Owners" enrich-modal toggle.
# These are the cases where the new upload's owner (executor / DM) is more
# authoritative than DataSift's existing record (which may still hold the
# deceased's name from a prior upload). For non-probate distressors the
# existing owner is typically correct and should be preserved.
SWAP_OWNERS_ON_NOTICE_TYPES: frozenset[str] = frozenset({
    "probate",
    "pre_probate",
})


def distressor_sort_key(notice_type: str) -> int:
    """Sort key for upload ordering. Unknown types sort last."""
    return DISTRESSOR_PRIORITY.get(notice_type, 99)


def should_swap_owners(notice_type: str) -> bool:
    """True iff this distressor should run enrich with Swap Owners ON."""
    return notice_type in SWAP_OWNERS_ON_NOTICE_TYPES

# Display-only labels for heir-row uploads in run summaries / Slack messages.
# These do NOT correspond to real DataSift lists — heir records land in
# NOTICE_TYPE_TO_LIST[notice_type] (same as DMs) and are tagged with
# ``heir_of_<notice_type>`` so filter presets can target them. Used by the
# run-summary formatter to display "→ Heirs of Foreclosure" instead of
# "→ Foreclosure" when reporting heir-CSV uploads.
HEIRS_DISPLAY_LABELS = {
    "foreclosure": "Heirs of Foreclosure",
    "probate": "Heirs of Probate",
    "pre_probate": "Heirs of Pre-Probate",
    "tax_sale": "Heirs of Tax Delinquent",
    "tax_delinquent": "Heirs of Tax Delinquent",
    "eviction": "Heirs of Eviction",
    "code_violation": "Heirs of Code Violation",
    "divorce": "Heirs of Divorce",
}


_PHONE_TIER_TAG = {
    "Dial First":  "phone_dial_first",
    "Dial Second": "phone_dial_second",
    "Dial Third":  "phone_dial_third",
    "Dial Fourth": "phone_dial_fourth",
    "Drop":        "phone_drop",
}
# Priority order for "best tier" selection — lower index wins
_PHONE_TIER_RANK = ["Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"]


def _collect_notice_phones(notice: NoticeData) -> list[str]:
    """Return every phone number attached to a notice (DM + heirs), in CSV column
    order. Phones are returned as raw strings — caller normalizes for lookup."""
    phones: list[str] = []
    for attr in (
        "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5",
        "landline_1", "landline_2", "landline_3",
    ):
        v = getattr(notice, attr, "") or ""
        if v.strip():
            phones.append(v.strip())
    return phones


def _phone_tier_tags(notice: NoticeData, phone_tiers: dict | None) -> list[str]:
    """Compute Trestle-derived tags for a notice based on its phone scores.

    Emits:
      - phone_dial_first / phone_dial_second / ... — BEST tier across all
        phones on the record (so a filter preset for `phone_dial_first` catches
        records where AT LEAST ONE phone is top-tier)
      - phone_litigator_risk — any phone flagged via Trestle litigator_checks
      - phone_unscored — record has phones but none have Trestle data yet

    Returns an empty list if the record has no phones at all (so we don't
    pollute non-phone records with phone_unscored).
    """
    if phone_tiers is None:
        phone_tiers = {}

    raw_phones = _collect_notice_phones(notice)
    if not raw_phones:
        return []

    from phone_validator import clean_phone  # local import — avoids hard dep

    out: list[str] = []
    seen_tiers: set[str] = set()
    has_score = False
    has_litigator = False

    for raw in raw_phones:
        cleaned = clean_phone(raw)
        info = phone_tiers.get(cleaned) if cleaned else None
        if not info:
            continue
        has_score = True
        tier = info.get("tier") or ""
        if tier in _PHONE_TIER_TAG:
            seen_tiers.add(tier)
        if info.get("is_litigator_risk"):
            has_litigator = True

    if not has_score:
        out.append("phone_unscored")
        return out

    # Pick the BEST tier across all phones on the record
    for tier in _PHONE_TIER_RANK:
        if tier in seen_tiers:
            out.append(_PHONE_TIER_TAG[tier])
            break

    if has_litigator:
        out.append("phone_litigator_risk")

    return out


# Friendly display names for the per-phone tier breakdown in Notes.
# Mirrors the column order in _collect_notice_phones() so the operator
# sees "Phone 1 (xxx-xxx-xxxx): Dial First" matching the actual CSV column.
_PHONE_FIELD_LABELS = [
    ("primary_phone", "Phone 1"),
    ("mobile_1",      "Phone 2"),
    ("mobile_2",      "Phone 3"),
    ("mobile_3",      "Phone 4"),
    ("mobile_4",      "Phone 5"),
    ("mobile_5",      "Phone 6"),
    ("landline_1",    "Phone 7"),
    ("landline_2",    "Phone 8"),
    ("landline_3",    "Phone 9"),
]


def _build_phone_tier_section(
    notice: NoticeData,
    phone_tiers: dict | None,
) -> str:
    """Build a per-phone tier breakdown for the Notes column.

    Output shape:

      === PHONE TIERS (dial order) ===
      Phone 1 (205-555-0001): Dial First  [litigator-risk]
      Phone 2 (205-555-0002): Dial Third
      Phone 3 (205-555-0003): unscored

    Phones are listed in CSV-column order so the operator can directly see
    which ``Phone N`` field to dial first. Litigator-risk flag is appended
    in brackets when present (TCPA exposure marker).

    Returns empty string if the record has no phones — caller decides
    whether to skip emitting the section header at all.
    """
    if phone_tiers is None:
        phone_tiers = {}

    try:
        from phone_validator import clean_phone
    except Exception:
        return ""

    lines: list[str] = []
    for attr, label in _PHONE_FIELD_LABELS:
        raw = (getattr(notice, attr, "") or "").strip()
        if not raw:
            continue
        cleaned = clean_phone(raw)
        info = phone_tiers.get(cleaned) if cleaned else None
        if info:
            tier = info.get("tier") or "unscored"
            litigator = info.get("is_litigator_risk")
            suffix = "  [litigator-risk]" if litigator else ""
            lines.append(f"{label} ({raw}): {tier}{suffix}")
        else:
            lines.append(f"{label} ({raw}): unscored")

    if not lines:
        return ""
    return "=== PHONE TIERS (dial order) ===\n" + "\n".join(lines)


def _build_tags(notice: NoticeData, phone_tiers: dict | None = None) -> str:
    """Build comma-separated tags string for DataSift upload.

    Tags include:
    - Courthouse Data (all records — for niche sequential filter presets)
    - notice_type (foreclosure, tax_sale, probate, tax_delinquent)
    - county (knox, blount)
    - YYYY-MM date tag
    - deceased/living status
    - DM confidence level (for deceased records)
    - has_auction if auction date is upcoming
    - phone_dial_first / phone_dial_second / phone_drop / phone_unscored —
      Trestle-derived best phone tier (when ``phone_tiers`` is passed in)
    - phone_litigator_risk — TCPA risk flag from Trestle litigator_checks
    """
    tags = ["Courthouse Data"]

    # Notice type
    if notice.notice_type:
        tags.append(notice.notice_type)

    # County
    if notice.county:
        tags.append(notice.county.lower())

    # Month tag from date_added
    if notice.date_added:
        try:
            dt = datetime.strptime(notice.date_added, "%Y-%m-%d")
            tags.append(dt.strftime("%Y-%m"))
        except ValueError:
            pass

    # Deceased/living status
    if notice.owner_deceased == "yes":
        tags.append("deceased")
        # DM confidence
        if notice.dm_confidence:
            tags.append(f"{notice.dm_confidence}_confidence")
    else:
        tags.append("living")

    # Upcoming auction
    if notice.auction_date:
        try:
            auction_dt = datetime.strptime(notice.auction_date, "%Y-%m-%d")
            if auction_dt >= datetime.now():
                tags.append("has_auction")
            # Per-date tag for DataSift filter presets that target a specific
            # foreclosure window (e.g. "show me everything auctioned this week").
            # Format: foreclosure_YYYY-MM-DD for foreclosure notices, otherwise
            # auction_YYYY-MM-DD (tax-sale uses tax_auction_YYYY-MM-DD).
            iso = auction_dt.strftime("%Y-%m-%d")
            if notice.notice_type == "foreclosure":
                tags.append(f"foreclosure_{iso}")
            elif notice.notice_type == "tax_sale":
                tags.append(f"tax_auction_{iso}")
            else:
                tags.append(f"auction_{iso}")
        except ValueError:
            pass

    # Tax delinquent flag + dollar-exposure ladder. Phase 1 focuses on dollar
    # amount as the primary distress signal; timeline-based tags are intentionally
    # absent because Madison's feed surfaces only current-year delinquencies
    # (older years are pruned after the May auction or redemption).
    if notice.tax_delinquent_amount:
        try:
            amt = float(notice.tax_delinquent_amount)
            if amt > 0:
                tags.append("tax_delinquent")
            if amt >= 5000:
                tags.append("tax_high_exposure")
            if amt >= 10000:
                tags.append("tax_high_exposure_10k")
        except (ValueError, TypeError):
            pass

    # Individual-vs-entity flag for tax records — reuse existing BUSINESS_RE
    # check so DataSift filter presets can target individuals only.
    if notice.notice_type in ("tax_sale", "tax_delinquent") and notice.owner_name:
        try:
            from config import BUSINESS_RE
            if not BUSINESS_RE.search(notice.owner_name):
                tags.append("individual_owner")
            else:
                tags.append("entity_owned")
        except ImportError:
            pass

    # Deep prospecting tags
    if notice.decision_maker_status == "verified_living":
        tags.append("dm_verified")
    if notice.heir_map_json:
        tags.append("has_heirs")
    elif notice.owner_deceased == "yes":
        tags.append("no_heirs")
    if (notice.owner_deceased == "yes"
            and notice.decision_maker_street
            and notice.decision_maker_street != notice.address):
        tags.append("has_dm_address")

    # Mailing-provenance flag (2026-06-21). When the DM-mailing pipeline
    # falls all the way to "use the property address as a placeholder"
    # (Tracerfy missed AND _lookup_dm_address waterfall missed too),
    # mark the row so direct-mail filter presets can de-prioritize —
    # mail goes to the deceased's home, not the DM's actual residence.
    # Door-knock sequences don't care (the knocker drives to the
    # property anyway).
    if getattr(notice, "dm_mailing_source", "") == "property_fallback":
        tags.append("mailing_unverified")

    # Tracerfy-skip surface (added 2026-06-27 — UX layer on Bug B fix
    # 843edbc). When tracerfy_skip_tracer skipped the contact before
    # batch submission (self-DM or no real DM mailing), tag the row so
    # the operator can spot it in DataSift's filter UI without trawling
    # log files. The specific-reason tag lets them route differently:
    #   `needs_dm_research`    — generic "manual research required"
    #   `dm_self_dm`           — decedent named themselves as DM in the
    #                            source notice; no separate executor was
    #                            in the obit. Operator may want to look
    #                            up the probate court filing directly.
    #   `dm_no_mailing`        — DM identified but their real mailing
    #                            address wasn't recoverable from any
    #                            people-search source. May resolve once
    #                            the property goes through formal probate
    #                            (the court will publish the PR's mailing).
    skip_reason = getattr(notice, "tracerfy_skip_reason", "")
    if skip_reason:
        tags.append("needs_dm_research")
        tags.append(f"dm_{skip_reason}")

    # Signing chain tags
    if notice.signing_chain_count:
        try:
            sc_count = int(notice.signing_chain_count)
            tags.append(f"signing_chain_{sc_count}")
            # Check if all signing heirs have phone data
            if notice.heir_map_json:
                import json as _json
                try:
                    heirs = _json.loads(notice.heir_map_json)
                    signers = [h for h in heirs
                               if h.get("signing_authority") and h.get("status") != "deceased"]
                    traced = [h for h in signers if h.get("phones")]
                    # DM #1 counts as traced if notice has primary_phone
                    if notice.primary_phone and signers:
                        dm1_name = (notice.decision_maker_name or "").lower()
                        if any(h.get("name", "").lower() == dm1_name for h in signers):
                            traced_names = {h.get("name", "").lower() for h in traced}
                            if dm1_name not in traced_names:
                                traced.append({"name": dm1_name})  # count DM #1
                    if traced and len(traced) >= len(signers):
                        tags.append("signing_chain_complete")
                    elif traced:
                        tags.append("signing_chain_partial")
                except (ValueError, TypeError):
                    pass
        except (ValueError, TypeError):
            pass

    # Entity research tags
    if notice.entity_type:
        tags.append("entity_owned")
        if notice.entity_person_name:
            tags.append("entity_researched")

    # Photo import tag (source_url starts with "photo:")
    if notice.source_url and notice.source_url.startswith("photo:"):
        tags.append("photo_import")

    # ── Tier classification (investor-target ZIP) ────────────────────
    # Every record runs through zip_tier() so DataSift filter presets can
    # sort/segment by tier without re-deriving from ZIP. "in_tier" is the
    # umbrella tag (matches BOTH tier_1 and tier_2) so a single filter
    # condition keeps the entire investor-target set; the granular tags
    # (tier_1 / tier_2) let presets target priority vs. secondary
    # separately. Off-tier records get NO tier tag at all — easy to spot
    # in DataSift via "no tier_1 AND no tier_2".
    if notice.zip:
        try:
            from target_zips import zip_tier
            t = zip_tier(notice.zip)
            if t == 1:
                tags.append("tier_1")
                tags.append("in_tier")
            elif t == 2:
                tags.append("tier_2")
                tags.append("in_tier")
        except ImportError:
            pass

    # ── AL probate enrichment tags (filter-preset friendly) ─────────
    # Municipality (Jefferson DispCode or Madison gap) — lowercased, spaces → underscores.
    # Birmingham metro core: birmingham, hoover, vestavia_hills, mountain_brook, homewood,
    # trussville. "county" flags unincorporated Jefferson (Dora-edge cases, etc.).
    if notice.municipality:
        muni_tag = notice.municipality.lower().replace(" ", "_")
        tags.append(f"municipality_{muni_tag}")

    # Homestead = primary residence (vs investment / vacant land)
    if notice.is_homestead == "Y":
        tags.append("homestead")

    # Notice subtype — the descriptive category (probate_sale, probate_creditors,
    # probate_heirs_notice, unsafe_building, etc.). Filter presets target this.
    if notice.notice_subtype:
        tags.append(notice.notice_subtype)

    # Tear-down signal — every parcel on a city's Unsafe-Building / Condemned
    # list is, by definition, a demolition candidate. Tag explicitly so the
    # outreach sequence frames the conversation around tear-down economics
    # (lot value, demo cost, build-back ARV) rather than rehab.
    if notice.notice_subtype == "unsafe_building":
        tags.append("demolish")

    # Early-distress signal — Birmingham Accela code-enforcement subtypes that
    # indicate soft maintenance failures (tall grass, junk vehicles, IPMC
    # violations). Owner is still in the property but slipping; outreach
    # framing is "rehab/clean-up offer" not "tear-down".
    if notice.notice_subtype in (
        "housing_enforcement",
        "inoperable_vehicle",
        "environmental_enforcement",
        "zoning_enforcement",
    ):
        tags.append("early_distress")

    # Multi-parcel estate — owner has 2+ parcels (rentals + family land)
    if notice.secondary_addresses:
        tags.append("multi_parcel")

    # Upcoming hearing (probate_sale) — make offer before court approval closes
    if notice.hearing_date:
        try:
            hd = datetime.strptime(notice.hearing_date, "%Y-%m-%d")
            days = (hd - datetime.now()).days
            if 0 <= days <= 30:
                tags.append("hearing_upcoming")
        except ValueError:
            pass

    # Creditor window still open (probate_creditors) — claims period not yet closed
    if notice.creditor_deadline:
        try:
            cd = datetime.strptime(notice.creditor_deadline, "%Y-%m-%d")
            if cd >= datetime.now():
                tags.append("creditor_window_open")
        except ValueError:
            pass

    # Trestle-derived phone tier tags (added last so they appear after the
    # subject-matter tags in the joined string, easier to spot at a glance)
    tags.extend(_phone_tier_tags(notice, phone_tiers))

    return ",".join(tags)


def _get_contact_info(notice: NoticeData) -> dict:
    """Determine the contact person and mailing address.

    For deceased owners with a decision maker: contact = DM (the live person
    we can actually market to). DM mailing address is used as-is; we do NOT
    fall back to the property address because that's the decedent's house,
    not the DM's — populating it as the DM's mailing pollutes skip-trace
    (the service would search for "JANE EXECUTOR @ 123 Dead Person Dr"
    instead of finding Jane's real address). Leaving mailing blank lets the
    skip-trace work on name + city/state context only.

    Self-DM guard: when the obit / probate extractor falsely set the DM name
    to the decedent's own name (happens when the obit lists no separate
    survivors), we fall through to the living-owner path so we don't end up
    marketing to a dead person under their own name.

    For living owners: contact = property owner. Mailing falls back to
    property address here (the living owner DOES live there in the common
    case — owner-occupied foreclosure / probate).

    For entity-owned properties: try tax_owner_name or DM as real person
    fallback.
    """
    if notice.owner_deceased == "yes" and notice.decision_maker_name:
        # Reject self-DM (DM name == decedent name) — fall through to the
        # living-owner path so the decedent's name lands in the owner field
        # as a last-resort identifier rather than a falsely-mapped "DM".
        dm_norm = notice.decision_maker_name.strip().lower()
        dec_norm = (notice.decedent_name or "").strip().lower()
        if not (dm_norm and dec_norm and dm_norm == dec_norm):
            first, last = _split_name(notice.decision_maker_name)
            # DM mailing — do NOT fall back to property address. State falls
            # back to the property state since the DM is most likely in the
            # same state as the inherited property and skip-trace needs
            # state context. Street/city/zip stay blank when unknown.
            street = notice.decision_maker_street
            city = notice.decision_maker_city
            state = notice.decision_maker_state or notice.state
            zip_code = notice.decision_maker_zip
            return {
                "first": first,
                "last": last,
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
            }

    # Living owner — try owner_name first
    first, last = _split_name(notice.owner_name)

    # If owner_name was an entity (LLC/Trust), try fallbacks for a real person
    if not first and not last:
        # Try entity research result (signing member, registered agent, etc.)
        if notice.entity_person_name:
            first, last = _split_name(notice.entity_person_name)
        # Try tax API owner name (sometimes has individual behind entity)
        if not first and not last:
            if notice.tax_owner_name and not _is_entity_name(notice.tax_owner_name):
                first, last = _split_name(notice.tax_owner_name)
        # Try decision maker (probate PR, etc.)
        if not first and not last and notice.decision_maker_name:
            first, last = _split_name(notice.decision_maker_name)

    street = notice.owner_street or notice.address
    city = notice.owner_city or notice.city
    state = notice.owner_state or notice.state
    zip_code = notice.owner_zip or notice.zip
    return {
        "first": first,
        "last": last,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
    }


def _build_heir_summary(notice: NoticeData) -> str:
    """Build signing chain + family summary from heir_map_json.

    Two sections:
    1. SIGNING CHAIN — heirs with signing_authority who must sign to sell property.
       Includes phone + address for each.
    2. OTHER FAMILY — everyone else (in-laws, step-children, etc.) in compact format.
    """
    if not notice.heir_map_json:
        return ""

    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not heirs:
        return ""

    # Split into signing chain vs others
    signers = [h for h in heirs
                if h.get("signing_authority") and h.get("status") != "deceased"]
    non_signers = [h for h in heirs if not h.get("signing_authority") or h.get("status") == "deceased"]

    lines = []

    # ── Signing chain section ──
    if signers:
        lines.append(f"=== SIGNING CHAIN ({len(signers)} heir{'s' if len(signers) != 1 else ''} must sign) ===")
        for i, h in enumerate(signers, 1):
            name = h.get("name", "?")
            rel = h.get("relationship", "unknown")
            status = h.get("status", "unverified")
            status_label = "ALIVE" if status == "verified_living" else status.upper()

            # Phone info
            phones = h.get("phones", [])
            # DM #1 phones are on flat NoticeData fields, not in heir_map_json
            if not phones and notice.primary_phone:
                dm1_name = (notice.decision_maker_name or "").strip().lower()
                if name.lower() == dm1_name:
                    phones = [notice.primary_phone]

            phone_str = phones[0] if phones else "no phone yet"
            lines.append(f"{i}. {name} ({rel}) — {status_label} — {phone_str}")

            # Address
            street = h.get("street", "")
            if street:
                city = h.get("city", "")
                # Fall back to the notice's own state when the heir record
                # didn't capture one (catches the "Birmingham, TN" bug for
                # AL records). Final fallback to "AL" since all active
                # pipelines are Alabama.
                state = h.get("state") or notice.state or "AL"
                zip_code = h.get("zip", "")
                addr_parts = [street]
                if city:
                    addr_parts.append(city)
                addr_parts.append(f"{state} {zip_code}".strip())
                lines.append(f"   Mail: {', '.join(addr_parts)}")
    else:
        lines.append("=== NO SIGNING CHAIN IDENTIFIED ===")

    # ── Non-signing family section (compact) ──
    if non_signers:
        entries = []
        for h in non_signers[:6]:
            name = h.get("name", "?")
            rel = h.get("relationship", "")
            status = h.get("status", "unverified")
            tag = "living" if status == "verified_living" else "deceased" if status == "deceased" else status
            entries.append(f"{name} ({rel}) [{tag}]")
        lines.append("")
        lines.append("=== OTHER FAMILY (no signing authority) ===")
        lines.append(", ".join(entries))
        remaining = len(non_signers) - 6
        if remaining > 0:
            lines.append(f"(+{remaining} more)")

    return "\n".join(lines)


def _build_dm_section(notice: NoticeData) -> str:
    """Build ranked decision maker section with status and address."""
    dms = []

    for i, (name_attr, rel_attr, status_attr) in enumerate([
        ("decision_maker_name", "decision_maker_relationship", "decision_maker_status"),
        ("decision_maker_2_name", "decision_maker_2_relationship", "decision_maker_2_status"),
        ("decision_maker_3_name", "decision_maker_3_relationship", "decision_maker_3_status"),
    ], 1):
        name = getattr(notice, name_attr, "")
        if not name:
            continue
        rel = getattr(notice, rel_attr, "") or "unknown"
        status = getattr(notice, status_attr, "") or "unverified"

        status_label = "VERIFIED LIVING" if status == "verified_living" else status
        line = f"{i}. {name} ({rel}) — {status_label}"

        # Include DM1 mailing address if available
        if i == 1 and notice.decision_maker_street:
            addr_parts = [notice.decision_maker_street]
            if notice.decision_maker_city:
                addr_parts.append(notice.decision_maker_city)
            if notice.decision_maker_state:
                addr_parts.append(notice.decision_maker_state)
            if notice.decision_maker_zip:
                addr_parts[-1] = addr_parts[-1] + " " + notice.decision_maker_zip
            line += f"\n   Mail: {', '.join(addr_parts)}"

        dms.append(line)

    if not dms:
        return ""

    return "=== DECISION MAKERS ===\n" + "\n".join(dms)


def _build_property_section(notice: NoticeData) -> str:
    """Build the property/notice details section for Notes."""
    parts = []

    # Include entity name when owner is LLC/Trust (name stripped from contact fields)
    if notice.owner_name and _is_entity_name(notice.owner_name):
        parts.append(f"Entity: {notice.owner_name}")

    # Include entity research contact if found
    if notice.entity_person_name:
        role = notice.entity_person_role.replace("_", " ").title() if notice.entity_person_role else "Unknown"
        parts.append(f"Entity Contact: {notice.entity_person_name} ({role})")

    if notice.notice_type:
        parts.append(notice.notice_type.replace("_", " ").title())

    if notice.auction_date:
        parts.append(f"Auction: {_format_date(notice.auction_date)}")

    if notice.tax_delinquent_amount:
        tax_str = f"Tax Due: ${notice.tax_delinquent_amount}"
        if notice.tax_delinquent_years:
            tax_str += f" ({notice.tax_delinquent_years} yrs)"
        parts.append(tax_str)

    # ── Probate-specific extras ──
    if notice.case_number:
        parts.append(f"Case#: {notice.case_number}")
    if notice.judge_name:
        parts.append(f"Judge: {notice.judge_name}")
    if notice.granted_date and notice.notice_type == "probate":
        parts.append(f"Letters Granted: {_format_date(notice.granted_date)}")
    if notice.creditor_deadline:
        parts.append(f"Creditor Deadline: {_format_date(notice.creditor_deadline)}")
    if notice.notice_subtype == "probate_sale":
        if notice.petition_filed_date:
            parts.append(f"Petition Filed: {_format_date(notice.petition_filed_date)}")
        if notice.hearing_date:
            parts.append(f"Hearing: {_format_date(notice.hearing_date)}")
        if notice.sale_type:
            parts.append(f"Sale Type: {notice.sale_type}")
    if notice.co_pr_names:
        parts.append(f"Co-PRs: {notice.co_pr_names}")
    if notice.heirs_named_in_notice and notice.notice_subtype == "probate_heirs_notice":
        parts.append(f"Named Heirs: {notice.heirs_named_in_notice}")
    if notice.secondary_addresses:
        parts.append(f"Additional Parcels: {notice.secondary_addresses}")
    if notice.total_estate_value:
        parts.append(f"Total Estate Value: ${notice.total_estate_value}")
    if notice.municipality:
        parts.append(f"Municipality: {notice.municipality}")

    # ── Foreclosure-specific extras ──
    if notice.notice_type == "foreclosure":
        if notice.mortgage_company:
            parts.append(f"Mortgagee: {notice.mortgage_company}")
        if notice.original_lender:
            parts.append(f"Original Lender: {notice.original_lender}")
        if notice.trustee:
            parts.append(f"Trustee: {notice.trustee}")
        if notice.trustee_file_number:
            parts.append(f"File#: {notice.trustee_file_number}")

    if notice.source_url:
        parts.append(f"Source: {notice.source_url}")

    return " | ".join(parts)


def _build_notes(notice: NoticeData, phone_tiers: dict | None = None) -> str:
    """Build a structured notes string for DataSift records.

    Deceased records get a multi-section format optimized for the call team:
      1. DECEASED OWNER          — identity + confidence
      2. DOD SANITY CHECK        — flags wrong-person matches / recent secondary deaths
      3. WHO MUST SIGN TO SELL   — structured signer table with locale + risk flags
      4. MASTER DIAL SHEET       — deduped phones across ALL signers, best-first,
                                   Trestle-tier-sorted, annotated with whose line
      5. HEIR VERIFICATION       — counts + names by verification status
      6. OTHER FAMILY            — non-signing kin (compact)
      7. PROPERTY                — situs details

    Rewrites the pre-2026-07-14 format (which showed one SIGNING CHAIN
    section with per-heir phones in ranked order) — the new format is
    based on the Deep Prospecting v4 skill, focused on the two questions
    every closer asks: "who has to sign?" and "which number do I dial
    first?" Both now have dedicated sections.

    ``phone_tiers`` is the {phone_str: {activity_score, assigned_tag, line_type, ...}}
    dict returned by phone_validator.score_phones_for_pipeline. When None
    or empty, the master dial sheet renders without tier ordering (falls
    back to source order) but still dedupes across signers.

    Living records get the same simple property-only format as before.
    """
    if notice.owner_deceased == "yes":
        sections = []

        # Section 1: Deceased owner header
        deceased_parts = []
        if notice.decedent_name:
            deceased_parts.append(f"Decedent: {notice.decedent_name}")
        if notice.date_of_death:
            deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
        if notice.obituary_url:
            deceased_parts.append(f"Obituary: {notice.obituary_url}")

        confidence_line = ""
        if notice.dm_confidence:
            confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
            if notice.dm_confidence_reason:
                confidence_line += f" — {notice.dm_confidence_reason}"

        if deceased_parts or confidence_line:
            header = "=== DECEASED OWNER ==="
            body = " | ".join(deceased_parts)
            if confidence_line:
                body += f"\n{confidence_line}" if body else confidence_line
            sections.append(f"{header}\n{body}")

        # Section 2: DOD sanity check (skill-defined guard — publication-anchored)
        dod_section = _build_dod_sanity(notice)
        if dod_section:
            sections.append(dod_section)

        # Section 3: WHO MUST SIGN TO SELL — structured signer table
        signer_table = _build_signer_table(notice)
        if signer_table:
            sections.append(signer_table)

        # Section 4: MASTER DIAL SHEET — deduped across all signers
        dial_sheet = _build_master_dial_sheet(notice, phone_tiers)
        if dial_sheet:
            sections.append(dial_sheet)

        # Section 5: HEIR VERIFICATION — grouped by status
        verify_section = _build_verification_summary(notice)
        if verify_section:
            sections.append(verify_section)

        # Section 6: OTHER FAMILY (compact — kept from prior format)
        other_family = _build_other_family_summary(notice)
        if other_family:
            sections.append(other_family)

        # Section 7: Property/notice details
        prop_section = _build_property_section(notice)
        if prop_section:
            sections.append(f"=== PROPERTY ===\n{prop_section}")

        if notice.report_url:
            sections.append(f"=== REPORT ===\n{notice.report_url}")

        return "\n\n".join(sections)

    # Living owner — simple format
    return _build_property_section(notice)


# ── New sections (2026-07-14 — Deep Prospecting v4 skill Phase A) ─────


def _normalize_phone_key(phone: str) -> str:
    """Return last 10 digits of a phone string, or '' if fewer than 10 digits."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def _classify_locale(heir: dict, notice: NoticeData) -> str:
    """Categorize where a heir lives relative to the subject property.

    Returns one of: "at property", "local", "in-state", "out-of-state",
    or "unknown". Used to steer the call team toward on-site + local
    signers first, and flag out-of-state signers as the usual closing
    bottleneck.

    "Local" means same county area — we approximate by matching the
    heir's city to the property's city (case-insensitive). For metro
    areas where the heir lives in a neighboring suburb we currently
    return "in-state" which is intentionally softer than "local".
    """
    heir_street = (heir.get("street") or "").strip().upper()
    heir_city = (heir.get("city") or "").strip().lower()
    heir_state = (heir.get("state") or "").strip().upper()
    prop_street = (notice.address or "").strip().upper()
    prop_city = (notice.city or "").strip().lower()
    prop_state = (notice.state or "AL").strip().upper()

    if heir_street and prop_street and heir_street == prop_street:
        return "at property"
    if not heir_state:
        return "unknown"
    if heir_state != prop_state:
        return "out-of-state"
    if heir_city and prop_city and heir_city == prop_city:
        return "local"
    return "in-state"


def _parse_publication_date(notice: NoticeData):
    """Best-effort publication date for the DOD sanity check.

    Prefers ``date_added`` (the pre-probate pipeline sets this to the
    obit's date of death OR today's harvest date — either way it's the
    "when did this lead enter our system" anchor). Falls back to
    ``received_date`` then today. Returns a date object or None.
    """
    from datetime import datetime, date as _date
    for candidate in (notice.received_date, notice.date_added):
        s = (candidate or "").strip()
        if not s:
            continue
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%-m/%-d/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    return _date.today()


def _build_dod_sanity(notice: NoticeData) -> str:
    """DOD sanity check anchored to publication date (Deep Prospecting v4).

    - OK: DOD within 3 years before publication (typical probate cadence)
    - FLAG (future DOD): DOD after publication — impossible, likely bad match
    - FLAG (>3yr): DOD > 3 years before publication — possible wrong-person
        OR a recent secondary death in the household triggered the filing
        (see skill: "recent household death changes the call opener")

    Returns "" if we don't have a parseable DOD.
    """
    from datetime import datetime
    dod_raw = (notice.date_of_death or "").strip()
    if not dod_raw:
        return ""
    dod = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            dod = datetime.strptime(dod_raw, fmt).date()
            break
        except ValueError:
            continue
    if not dod:
        return ""

    pub = _parse_publication_date(notice)
    if not pub:
        return ""

    gap_days = (pub - dod).days
    gap_yrs = gap_days / 365.25

    lines = ["=== DOD SANITY CHECK ==="]
    lines.append(f"Published: {pub.isoformat()}  ·  DOD: {dod.isoformat()}  ·  Gap: {gap_yrs:+.1f}yr")

    if gap_days < 0:
        lines.append(
            f"⚠ FLAG — DOD is AFTER publication date. Impossible; likely a "
            f"wrong-person obituary match. Verify decedent identity before dialing."
        )
    elif gap_yrs > 3.0:
        lines.append(
            f"⚠ FLAG — DOD is {gap_yrs:.1f} years before publication (>3yr). "
            f"Possibilities: wrong-person match, OR a recent household death "
            f"(e.g. surviving spouse) triggered this filing. The heir set below "
            f"likely still stands; open with condolences for the recent loss "
            f"rather than the decades-old estate."
        )
    else:
        lines.append("✓ OK — DOD within normal probate cadence.")

    # Enformion vs obit DOD conflict — set by the Enformion resolver via
    # the "dod_conflict" flag in missing_data_flags. Surface here so the
    # closer sees it before dialing.
    if "dod_conflict" in (notice.missing_data_flags or ""):
        lines.append(
            "⚠ FLAG — Enformion death index DOD disagrees with obituary/notice DOD "
            "by 1+ year. Common cause: original owner died long ago, a family "
            "member maintained the home, and THAT person's recent death triggered "
            "this filing. The heir set stands; the estate currently in probate "
            "may belong to the recent decedent. Verify with the closer."
        )

    return "\n".join(lines)


def _build_signer_table(notice: NoticeData) -> str:
    """Structured 'who must sign to sell' table with locale + risk flags."""
    if not notice.heir_map_json:
        return ""
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not heirs:
        return ""

    signers = [
        h for h in heirs
        if h.get("signing_authority") and h.get("status") != "deceased"
    ]
    if not signers:
        return "=== WHO MUST SIGN TO SELL ===\nNo signing-authority heirs identified. Manual research required."

    lines = [f"=== WHO MUST SIGN TO SELL ({len(signers)} heir{'s' if len(signers) != 1 else ''}) ==="]

    # Header
    lines.append(
        f"{'Heir':<32} {'Rel':<10} {'Locale':<22} Sig Req?"
    )
    lines.append("─" * 78)

    has_out_of_state = False
    has_spouse = False
    for h in signers:
        name = (h.get("name") or "?")[:31]
        rel = (h.get("relationship") or "unknown")[:9]
        locale = _classify_locale(h, notice)
        if locale == "out-of-state":
            locale_str = f"{(h.get('city') or '?')}, {h.get('state') or '?'} ⚠"
            has_out_of_state = True
        elif locale == "at property":
            locale_str = "at property"
        elif locale == "local":
            locale_str = f"{h.get('city') or '?'} (local)"
        elif locale == "in-state":
            locale_str = f"{h.get('city') or '?'}, {h.get('state') or 'AL'}"
        else:
            locale_str = "unknown"
        locale_str = locale_str[:21]
        if rel.lower() == "spouse":
            has_spouse = True
        lines.append(f"{name:<32} {rel:<10} {locale_str:<22} REQUIRED")

    # Signing risk flags — the skill's "flags to verify" checklist
    flag_lines = []
    if has_spouse:
        flag_lines.append(
            "· Surviving spouse present — takes intestate share; cannot close without their signature"
        )
    if has_out_of_state:
        flag_lines.append(
            "· Out-of-state signer(s) — usual closing bottleneck; engage early"
        )
    deceased_signers = [
        h for h in heirs
        if h.get("signing_authority") and h.get("status") == "deceased"
    ]
    if deceased_signers:
        flag_lines.append(
            f"· {len(deceased_signers)} signing-authority heir(s) deceased — "
            f"their share passes to THEIR children (per stirpes); adds signers"
        )
    unverified = [h for h in signers if h.get("status") != "verified_living"]
    if unverified:
        flag_lines.append(
            f"· {len(unverified)}/{len(signers)} signer(s) unverified — "
            f"confirm alive status before extensive skip-trace investment"
        )
    flag_lines.append(
        "· No will detected in probate index — verify (a will would name "
        "an executor and override equal-share intestate split)"
    )

    if flag_lines:
        lines.append("")
        lines.append("Signing risk flags (verify before closing — none are legal conclusions):")
        lines.extend(flag_lines)

    return "\n".join(lines)


def _collect_phones_with_reach(notice: NoticeData) -> list[dict]:
    """Walk all sources → list of {phone, sources, is_dm1} deduped by digits.

    Sources include:
      · Flat NoticeData fields (primary_phone / mobile_1-5 / landline_1-3),
        attributed to DM #1
      · heir_map_json[].phones[] on every signing-authority heir

    Multiple heirs sharing a household landline collapse into ONE entry
    with all sharers listed under "reaches".
    """
    dm1_name = (notice.decision_maker_name or "").strip() or "DM #1"

    by_key: dict[str, dict] = {}   # normalized digit key → entry

    def _add(raw_phone: str, reach: str, sources: list[str] | None = None):
        raw = (raw_phone or "").strip()
        if not raw:
            return
        key = _normalize_phone_key(raw)
        if not key:
            return
        entry = by_key.get(key)
        if entry is None:
            by_key[key] = {
                "phone": raw, "reaches": [reach],
                "sources": list(sources) if sources else [],
            }
        else:
            if reach not in entry["reaches"]:
                entry["reaches"].append(reach)
            if sources:
                for s in sources:
                    if s not in entry["sources"]:
                        entry["sources"].append(s)

    # Walk heir_map_json FIRST so per-heir phones get the correct attribution.
    # Then flat slots (which are attributed to DM #1) only add reaches for
    # phones NOT already in the heir map — those came from a DM-specific
    # source (Tracerfy DM #1 or Enformion's DM #1 side of the merge).
    heir_phone_keys: set[str] = set()
    if notice.heir_map_json:
        try:
            heirs = json.loads(notice.heir_map_json)
        except (json.JSONDecodeError, TypeError):
            heirs = []
        for h in heirs:
            if not h.get("signing_authority") or h.get("status") == "deceased":
                continue
            heir_name = (h.get("name") or "?").strip()
            # phone_sources sidecar dict (added 2026-07-16 — vendor comparison)
            heir_phone_sources = h.get("phone_sources") or {}
            for p in (h.get("phones") or []):
                phone_str = ""
                if isinstance(p, str):
                    phone_str = p
                elif isinstance(p, dict):
                    phone_str = p.get("phone_number") or p.get("phone") or ""
                if phone_str:
                    heir_phone_keys.add(_normalize_phone_key(phone_str))
                    srcs = heir_phone_sources.get(phone_str)
                    _add(phone_str, heir_name, sources=srcs)

    # Flat slots: attribute to DM #1 ONLY when the phone isn't already
    # covered by a per-heir entry above. Prevents phones promoted from
    # Enformion (which live per-heir in heir_map_json) from also being
    # attributed to DM #1 via the flat slot. These flat-slot phones came
    # from Tracerfy (the only source that populates flat slots directly).
    for attr in (
        "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
        "mobile_5", "landline_1", "landline_2", "landline_3",
    ):
        val = getattr(notice, attr, "") or ""
        if val and _normalize_phone_key(val) not in heir_phone_keys:
            _add(val, dm1_name, sources=["tracerfy"])

    return list(by_key.values())


_TIER_ORDER = {
    "Dial First": 0, "Dial Second": 1, "Dial Third": 2,
    "Dial Fourth": 3, "Drop": 4,
}

# Precision-focused display caps (added 2026-07-16 — operator flagged
# "I don't need 89 phones per person; I want the highest-probability
# working number"). We show only the top-tier phones in the Notes
# Master Dial Sheet; lower-tier phones stay in heir_map_json for
# reference but don't crowd the closer's view.
_DIAL_SHEET_MAX_ROWS = 10          # cap Master Dial Sheet display
_DIAL_SHEET_TIER_FLOOR = "Dial Third"   # exclude Dial Fourth + Drop from display


def _source_badge(sources: list[str]) -> str:
    """Compact source label for the Master Dial Sheet 'Sources' column.

    - "Tracerfy + Enformion"  → corroborated by both (highest confidence)
    - "Enformion" / "Tracerfy" → single source
    - "" → unknown provenance (older heir_map_json without phone_sources)
    """
    if not sources:
        return ""
    if len(sources) == 1:
        return sources[0].capitalize()
    # Corroborated — join in canonical order (Tracerfy first if present)
    ordered = []
    for s in ("tracerfy", "enformion", "datasift"):
        if s in sources:
            ordered.append(s.capitalize())
    for s in sources:
        cap = s.capitalize()
        if cap not in ordered:
            ordered.append(cap)
    return " + ".join(ordered)


def _build_master_dial_sheet(notice: NoticeData, phone_tiers: dict | None) -> str:
    """Deduped, best-first dial list across ALL signers with tier + source.

    Precision-focused (2026-07-16): filters to top-tier phones only
    (default Dial First / Dial Second / Dial Third — see _DIAL_SHEET_TIER_FLOOR),
    caps display to _DIAL_SHEET_MAX_ROWS. Lower-tier phones stay in
    heir_map_json for reference but don't clutter the closer's view.

    Sort priority within the displayed set:
      1. Trestle tier (Dial First → Dial Third)
      2. Corroboration (phones found by 2+ sources > single source —
         cross-source agreement is a strong accuracy signal)
      3. Line type (Mobile > Landline > Voip)
      4. Trestle activity score (higher first, tiebreak within tier)

    When phone_tiers is empty or None (Trestle disabled), still emits the
    deduped list but without tier filtering — falls back to source order.
    """
    entries = _collect_phones_with_reach(notice)
    if not entries:
        heirs_json = notice.heir_map_json
        if heirs_json and any(
            h.get("signing_authority") for h in (json.loads(heirs_json) or [])
        ):
            return (
                "=== MASTER DIAL SHEET ===\n"
                "No phones recovered for any signing-authority heir. "
                "Tracerfy + Enformion both returned no matches for this family. "
                "Consider manual research via TruePeopleSearch / FastPeopleSearch / "
                "CyberBackgroundChecks, or DataSift's post-upload Skip Trace pass."
            )
        return ""

    phone_tiers = phone_tiers or {}

    def _tier_data(phone: str) -> dict:
        if phone in phone_tiers:
            return phone_tiers[phone] or {}
        key = _normalize_phone_key(phone)
        for k, v in phone_tiers.items():
            if _normalize_phone_key(k) == key:
                return v or {}
        return {}

    # Enrich each entry with Trestle tier + line type
    for e in entries:
        td = _tier_data(e["phone"])
        e["tier"] = td.get("assigned_tag") or ""
        e["score"] = td.get("activity_score")
        e["line_type"] = td.get("line_type") or ""
        e["is_litigator_risk"] = td.get("is_litigator_risk")
        # sources already populated by _collect_phones_with_reach
        e.setdefault("sources", [])

    # Line-type priority (Mobile beats Landline beats Voip for reachability)
    line_order = {"mobile": 0, "landline": 1, "voip": 2, "": 9}

    def _sort_key(e):
        tier_rank = _TIER_ORDER.get(e["tier"], 99)
        # Corroborated (2+ sources) sorts BEFORE single-source at same tier
        corrob_rank = 0 if len(e["sources"]) >= 2 else 1
        lt_rank = line_order.get((e["line_type"] or "").lower(), 9)
        score = e.get("score") or 0
        return (tier_rank, corrob_rank, lt_rank, -score)

    entries.sort(key=_sort_key)

    tier_floor_rank = _TIER_ORDER.get(_DIAL_SHEET_TIER_FLOOR, 99)
    displayable = [e for e in entries
                   if _TIER_ORDER.get(e["tier"], 99) <= tier_floor_rank]
    below_floor = [e for e in entries
                   if _TIER_ORDER.get(e["tier"], 99) > tier_floor_rank]
    truncated = displayable[_DIAL_SHEET_MAX_ROWS:]
    displayable = displayable[:_DIAL_SHEET_MAX_ROWS]

    # Source-mix summary — 1-line snapshot of vendor contribution
    src_counter: dict[str, int] = {}
    corroborated = 0
    for e in entries:
        srcs = e.get("sources") or []
        if len(srcs) >= 2:
            corroborated += 1
        for s in srcs:
            src_counter[s] = src_counter.get(s, 0) + 1
    src_summary_bits = [f"{s.capitalize()}={ct}" for s, ct in
                        sorted(src_counter.items(), key=lambda x: -x[1])]
    if corroborated:
        src_summary_bits.append(f"corroborated={corroborated}")
    src_summary = "  ·  ".join(src_summary_bits) if src_summary_bits else "no source data"

    header = (
        f"=== MASTER DIAL SHEET  "
        f"({len(entries)} unique · showing top {len(displayable)} "
        f"at or above {_DIAL_SHEET_TIER_FLOOR}) ==="
    )
    lines = [header]
    lines.append(f"Source mix: {src_summary}")
    lines.append("")
    lines.append(f"{'Phone':<16}{'Tier':<13}{'Type':<10}{'Reaches':<26}Sources")
    lines.append("─" * 88)

    for e in displayable:
        phone_disp = e["phone"][:15]
        tier = (e["tier"] or "(unscored)")[:12]
        line_type = (e["line_type"] or "?")[:9]
        reaches = ", ".join(e["reaches"])[:25]
        sources_disp = _source_badge(e.get("sources") or [])[:22]
        marker = ""
        if e["is_litigator_risk"]:
            marker = " ⚠LITIGATOR"
        lines.append(
            f"{phone_disp:<16}{tier:<13}{line_type:<10}{reaches:<26}"
            f"{sources_disp}{marker}"
        )

    hidden = len(truncated) + len(below_floor)
    if hidden:
        parts = []
        if truncated:
            parts.append(f"{len(truncated)} additional at-or-above-tier")
        if below_floor:
            parts.append(f"{len(below_floor)} below {_DIAL_SHEET_TIER_FLOOR}")
        lines.append("")
        lines.append(f"(+{hidden} more phones hidden — {'; '.join(parts)}. "
                     f"Full list in heir_map_json / CSV columns.)")

    if not phone_tiers:
        lines.append("")
        lines.append(
            "(Trestle scoring unavailable — phones listed in source order, "
            "tier filter skipped. Set TRESTLE_API_KEY to enable priority ordering.)"
        )

    return "\n".join(lines)


def _build_verification_summary(notice: NoticeData) -> str:
    """Heir verification counts grouped by status."""
    if not notice.heir_map_json:
        return ""
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not heirs:
        return ""

    signers = [h for h in heirs if h.get("signing_authority")]
    if not signers:
        return ""

    living = [h for h in signers if h.get("status") == "verified_living"]
    deceased = [h for h in signers if h.get("status") == "verified_deceased"
                or h.get("status") == "deceased"]
    unverified = [h for h in signers if h.get("status") not in (
        "verified_living", "verified_deceased", "deceased",
    )]

    lines = ["=== HEIR VERIFICATION STATUS ==="]
    if living:
        names = ", ".join(h.get("name", "?") for h in living)
        lines.append(f"✓ VERIFIED LIVING ({len(living)}): {names}")
    if deceased:
        names = ", ".join(h.get("name", "?") for h in deceased)
        lines.append(f"✗ VERIFIED DECEASED ({len(deceased)}): {names}")
    if unverified:
        names = ", ".join(h.get("name", "?") for h in unverified)
        lines.append(
            f"? UNVERIFIED ({len(unverified)}): {names}\n"
            f"    → Currently no verification source integrated. Enformion "
            f"(Phase B) will verify each signer's alive status."
        )
    return "\n".join(lines)


def _build_other_family_summary(notice: NoticeData) -> str:
    """Compact list of non-signing kin — carried over from prior format."""
    if not notice.heir_map_json:
        return ""
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    non_signers = [h for h in heirs if not h.get("signing_authority")
                   or h.get("status") == "deceased"]
    if not non_signers:
        return ""
    entries = []
    for h in non_signers[:6]:
        name = h.get("name", "?")
        rel = h.get("relationship", "")
        status = h.get("status", "unverified")
        tag = ("living" if status == "verified_living"
               else "deceased" if status in ("deceased", "verified_deceased")
               else "unverified")
        entries.append(f"{name} ({rel}) [{tag}]")
    result = ["=== OTHER FAMILY (no signing authority) ===",
              ", ".join(entries)]
    remaining = len(non_signers) - 6
    if remaining > 0:
        result.append(f"(+{remaining} more)")
    return "\n".join(result)


def _build_dm_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 1: deceased owner header + DM breakdown + property.

    For living records, returns the simple property section.
    Used by write_datasift_split_csvs() for the DMs upload.
    """
    if notice.owner_deceased != "yes":
        return _build_property_section(notice)

    sections = []

    # Deceased owner header
    deceased_parts = []
    if notice.decedent_name:
        deceased_parts.append(f"Decedent: {notice.decedent_name}")
    if notice.date_of_death:
        deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
    if notice.obituary_url:
        deceased_parts.append(f"Obituary: {notice.obituary_url}")

    confidence_line = ""
    if notice.dm_confidence:
        confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
        if notice.dm_confidence_reason:
            confidence_line += f" — {notice.dm_confidence_reason}"

    if deceased_parts or confidence_line:
        header = "=== DECEASED OWNER ==="
        body = " | ".join(deceased_parts)
        if confidence_line:
            body += f"\n{confidence_line}" if body else confidence_line
        sections.append(f"{header}\n{body}")

    # Decision makers
    dm_section = _build_dm_section(notice)
    if dm_section:
        sections.append(dm_section)

    # Property details
    prop_section = _build_property_section(notice)
    if prop_section:
        sections.append(f"=== PROPERTY ===\n{prop_section}")

    return "\n\n".join(sections)


def _build_heir_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 2: deceased-owner header + heir map.

    Used by write_datasift_split_csvs() for the Heirs upload. Mirrors
    the DMs CSV's deceased-owner header (decedent name, DoD, obituary
    URL, confidence) so heir rows carry the same context — operator
    reported the Heirs Notes were too sparse on 2026-06-10 with no
    signing-order context. Returns empty string if no heir data AND no
    deceased-owner context (shouldn't happen for rows that make it into
    the Heirs CSV — the writer filters on owner_deceased=="yes" AND
    heir_map_json).
    """
    sections = []

    # Deceased owner header — same shape as _build_dm_notes for parity
    deceased_parts = []
    if notice.decedent_name:
        deceased_parts.append(f"Decedent: {notice.decedent_name}")
    if notice.date_of_death:
        deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
    if notice.obituary_url:
        deceased_parts.append(f"Obituary: {notice.obituary_url}")

    confidence_line = ""
    if notice.dm_confidence:
        confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
        if notice.dm_confidence_reason:
            confidence_line += f" — {notice.dm_confidence_reason}"

    if deceased_parts or confidence_line:
        header = "=== DECEASED OWNER ==="
        body = " | ".join(deceased_parts)
        if confidence_line:
            body += f"\n{confidence_line}" if body else confidence_line
        sections.append(f"{header}\n{body}")

    # Heir map (signing chain + other family)
    heir_section = _build_heir_summary(notice)
    if heir_section:
        sections.append(heir_section)

    return "\n\n".join(sections)


def _validate_row(row: dict) -> tuple[bool, list[str]]:
    """Check a row dict for DataSift completeness.

    DataSift marks records incomplete when missing owner first/last name,
    mailing address, or property address.

    Returns:
        (is_complete, issues) — True if record will be "clean" in DataSift.
    """
    issues = []
    if not row.get("Owner First Name"):
        issues.append("no_first_name")
    if not row.get("Owner Last Name"):
        issues.append("no_last_name")
    if not row.get("Property Street Address"):
        issues.append("no_property_address")
    if not row.get("Mailing Street Address"):
        issues.append("no_mailing_address")
    return (len(issues) == 0, issues)


def _build_row(
    notice: NoticeData,
    notes_override: str | None = None,
    is_heir_row: bool = False,
    phone_tiers: dict | None = None,
) -> dict:
    """Build a single CSV row dict for a NoticeData record.

    Args:
        notice: The notice to format.
        notes_override: If provided, use this as the Notes value instead of
            calling _build_notes(). Used by write_datasift_split_csvs().
        is_heir_row: When True, appends a ``heir_of_<notice_type>`` tag to
            the Tags column so DataSift filter presets can target "Heirs of
            Foreclosure" / "Heirs of Probate" / etc. without changing which
            list the row belongs to. Set by the Heirs CSV writer in
            write_datasift_split_csvs().
        phone_tiers: Optional ``{cleaned_phone: {"tier": str, ...}}`` dict
            from Trestle (via ``full_pipeline.result.phone_tiers``). When
            provided, the row's Tags column gets phone_dial_first /
            phone_dial_second / phone_drop / phone_unscored tags plus
            phone_litigator_risk when applicable.

    Returns:
        Dict keyed by DATASIFT_COLUMNS headers.
    """
    contact = _get_contact_info(notice)

    # Tags strategy (rewritten 2026-06-11 per operator request).
    #
    # The DataSift upload wizard's Step-4 column-mapping for the "Tags"
    # column has been chronically unreliable (operator reports across
    # 2026-06-08 → 06-10 confirm every upload silently drops descriptive
    # tags). To stop losing this signal, we split the tag set into two
    # surfaces:
    #
    #  * **Tags column → "Courthouse Data" only.** A single value the
    #    wizard's Step-2 typeahead can reliably set (and Step-4 won't
    #    drop because there's only one comma-separated value).
    #
    #  * **Notes column (Column AA) → full descriptive tag list appended
    #    at the bottom under "=== TAGS ===".** Notes maps reliably in
    #    Step-4 because the wizard auto-detects it as a free-text field.
    #    Downstream filter presets need to be rewritten to search the
    #    Notes content via "contains" instead of pure Tag filters —
    #    operator confirmed acceptable trade-off for reliability.
    full_tag_list = _build_tags(notice, phone_tiers=phone_tiers)
    if is_heir_row and notice.notice_type:
        heir_tag = f"heir_of_{notice.notice_type}"
        full_tag_list = (
            f"{full_tag_list},{heir_tag}" if full_tag_list else heir_tag
        )

    tags = "Courthouse Data"
    list_name = NOTICE_TYPE_TO_LIST.get(notice.notice_type, "")
    notes = notes_override if notes_override is not None else _build_notes(
        notice, phone_tiers=phone_tiers,
    )

    # Per-phone column block (Phone N + Phone Type N + Phone Status N +
    # Phone Tags N) — operator request 2026-06-12 after reviewing the
    # DataSift upload template. The Trestle tier for EACH phone now lands
    # in its own Phone Tags N column rather than a single record-level tag
    # in the Tags column, so the cold-caller sees dial order directly in
    # the DataSift record's phone block.
    phone_cols: dict[str, str] = {}
    if phone_tiers is None:
        phone_tiers = {}
    try:
        from phone_validator import clean_phone
    except Exception:
        clean_phone = lambda s: ""  # noqa: E731 — defensive degrade
    for attr, label in _PHONE_FIELD_LABELS:
        # label is "Phone N"; the Type/Status/Tags columns are named
        # "Phone Type N", "Phone Status N", "Phone Tags N" (number AFTER
        # the field name). Extract the number to build the correct keys.
        phone_n = label.split(" ", 1)[1]
        type_col = f"Phone Type {phone_n}"
        status_col = f"Phone Status {phone_n}"
        tags_col = f"Phone Tags {phone_n}"

        raw = (getattr(notice, attr, "") or "").strip()
        phone_cols[label] = raw
        if not raw:
            phone_cols[type_col] = ""
            phone_cols[status_col] = ""
            phone_cols[tags_col] = ""
            continue
        cleaned = clean_phone(raw)
        info = phone_tiers.get(cleaned) if cleaned else None
        if info:
            line_type = (info.get("line_type") or "").strip()
            is_valid = info.get("is_valid")
            tier = (info.get("tier") or "").strip()
            phone_cols[type_col] = line_type
            if is_valid is True:
                phone_cols[status_col] = "Valid"
            elif is_valid is False:
                phone_cols[status_col] = "Invalid"
            else:
                phone_cols[status_col] = ""
            # Phone Tags N — primarily the tier (Dial First / etc.). If
            # litigator-risk fires on this specific phone, append it as
            # a second comma-separated tag.
            tag_parts: list[str] = []
            if tier and tier != "Unknown":
                tag_parts.append(tier)
            if info.get("is_litigator_risk"):
                tag_parts.append("litigator_risk")
            phone_cols[tags_col] = ", ".join(tag_parts)
        else:
            phone_cols[type_col] = ""
            phone_cols[status_col] = ""
            phone_cols[tags_col] = ""

    # Per-phone Trestle tier breakdown (operator request 2026-06-12). The
    # record-level "best tier" tag in the TAGS section answers "is this
    # record worth dialing" but not "WHICH Phone N to dial first". This
    # section spells out each phone's tier in CSV-column order so the
    # operator can pick the right number at a glance.
    phone_section = _build_phone_tier_section(notice, phone_tiers)
    if phone_section:
        notes = f"{notes}\n\n{phone_section}" if notes else phone_section

    # Append the full descriptive tag list to Notes so it survives the
    # upload regardless of Step-4 Tags column-mapping behaviour.
    if full_tag_list:
        notes = (
            f"{notes}\n\n=== TAGS ===\n{full_tag_list}"
            if notes else f"=== TAGS ===\n{full_tag_list}"
        )

    # Conditionally map auction_date to the right built-in field
    tax_auction = ""
    foreclosure_date = ""
    probate_open = ""
    if notice.notice_type == "tax_sale":
        tax_auction = _format_date(notice.auction_date)
    elif notice.notice_type == "foreclosure":
        foreclosure_date = _format_date(notice.auction_date)
    elif notice.notice_type == "probate":
        # Prefer granted_date (when Letters issued) over publication date — more accurate
        probate_open = _format_date(notice.granted_date or notice.date_added)

    # Personal Representative for probate — prefer the parsed PR (owner_name)
    # over the obituary-derived decision_maker_name. owner_name is the verified
    # PR named in the Letters Testamentary; decision_maker_name may be a
    # surviving heir derived from the obituary.
    personal_rep = ""
    if notice.notice_type == "probate":
        personal_rep = notice.owner_name or notice.decision_maker_name

    # Fallbacks — assessor data when Zillow enrichment hasn't run
    estimated_value = notice.estimated_value or notice.assessed_value
    structure_type = notice.property_type or notice.property_use

    # Property City fallback (2026-06-23): Jefferson E-Ring API sometimes
    # returns a populated ZIP but EMPTY situs_city. Without this fallback,
    # 8/19 pre-probate rows on 6/23 shipped with blank Property City.
    # Lookup uses USPS-preferred city for the known Tier 1+2 ZIPs across
    # Jefferson/Madison/Marshall — see target_zips.city_for_zip.
    property_city = notice.city
    if not property_city and notice.zip:
        try:
            from target_zips import city_for_zip
            property_city = city_for_zip(notice.zip)
        except Exception:
            pass

    # Mailing City fallback (2026-06-25): same shape as Property City —
    # when contact["city"] is empty but contact["zip"] is in our Tier 1+2
    # set, fall back to USPS-preferred city. Operator-reported case:
    # 5933 SHILO RUN (probate) — DM (Linda Wanninger) inherited the
    # property and mails there, but the probate notice parser captured
    # the street + ZIP without a city → mailing landed as
    # "5933 SHILO RUN, , AL 35126" → skip-trace requires Mailing City to
    # find phones → Phone 1 shipped empty even though the DM is real and
    # findable. Falling back via city_for_zip(35126)='Pinson' closes the
    # gap for known target ZIPs without polluting off-target rows.
    mailing_city = contact["city"]
    if not mailing_city and contact["zip"]:
        try:
            from target_zips import city_for_zip
            mailing_city = city_for_zip(contact["zip"])
        except Exception:
            pass

    return {
        # ── Core auto-mapped ──
        "Property Street Address": notice.address,
        "Property City": property_city,
        "Property State": notice.state or "AL",
        "Property ZIP Code": notice.zip,
        "Owner First Name": contact["first"],
        "Owner Last Name": contact["last"],
        "Mailing Street Address": contact["street"],
        "Mailing City": mailing_city,
        "Mailing State": contact["state"],
        "Mailing ZIP Code": contact["zip"],
        # ── Phone/Email — Phone N populated via phone_cols dict above,
        # which also fills Phone Type N / Phone Status N / Phone Tags N
        # from the per-phone Trestle data. Merged into the row dict via
        # **phone_cols at return time below.
        "Email 1": notice.email_1,
        "Email 2": notice.email_2,
        "Email 3": notice.email_3,
        "Email 4": notice.email_4,
        "Email 5": notice.email_5,
        "Tags": tags,
        "Lists": list_name,
        "Notes": notes,
        # ── Built-in fields ──
        "Estimated Value": estimated_value,
        "MSL Status": notice.mls_status,
        "Last Sale Date": _format_date(notice.mls_last_sold_date),
        "Last Sale Price": notice.mls_last_sold_price,
        "Equity Percentage": notice.equity_percent,
        "Tax Deliquent Value": notice.tax_delinquent_amount,
        "Tax Delinquent Year": notice.tax_delinquent_years,
        "Tax Auction Date": tax_auction,
        "Foreclosure Date": foreclosure_date,
        "Probate Open Date": probate_open,
        "Personal Representative": personal_rep,
        "Parcel ID": notice.parcel_id,
        "Structure Type": structure_type,
        "Year Built": notice.year_built,
        "Living SqFt": notice.sqft,
        "Bedrooms": notice.bedrooms,
        "Bathrooms": notice.bathrooms,
        "Lot (Acres)": notice.lot_size,
        # ── Custom fields (SiftStack group) ──
        "Notice Type": notice.notice_type,
        "County": notice.county,
        "Date Added": _format_date(notice.date_added),
        "Owner Deceased": notice.owner_deceased,
        "Date of Death": notice.date_of_death,
        "Decedent Name": notice.decedent_name,
        "Decision Maker": notice.decision_maker_name,
        "DM Relationship": notice.decision_maker_relationship,
        "DM Confidence": notice.dm_confidence,
        "DM 2 Name": notice.decision_maker_2_name,
        "DM 2 Relationship": notice.decision_maker_2_relationship,
        "DM 3 Name": notice.decision_maker_3_name,
        "DM 3 Relationship": notice.decision_maker_3_relationship,
        "Obituary URL": notice.obituary_url,
        "Source URL": notice.source_url,
        # ── Deep prospecting fields ──
        "DM 1 Status": notice.decision_maker_status,
        "DM 1 Source": notice.decision_maker_source,
        "DM 2 Status": notice.decision_maker_2_status,
        "DM 3 Status": notice.decision_maker_3_status,
        "Heir Count": _heir_count(notice),
        "Heirs Living": notice.heirs_verified_living,
        "Signing Chain Count": notice.signing_chain_count,
        "Signing Chain Names": notice.signing_chain_names,
        "DM Confidence Reason": notice.dm_confidence_reason,
        "Data Flags": notice.missing_data_flags,
        # ── Entity research fields ──
        "Entity Type": notice.entity_type,
        "Entity Contact": notice.entity_person_name,
        "Entity Contact Role": notice.entity_person_role,
        # ── AL probate enrichment ──
        "Probate Case Number": notice.case_number,
        "Judge of Probate": notice.judge_name,
        "Probate Subtype": notice.notice_subtype,
        "Petition Filed Date": _format_date(notice.petition_filed_date),
        "Hearing Date": _format_date(notice.hearing_date),
        "Creditor Claim Deadline": _format_date(notice.creditor_deadline),
        "Total Estate Value": notice.total_estate_value,
        # Merge in Phone N / Phone Type N / Phone Status N / Phone Tags N
        # from the phone_cols dict built above.
        **phone_cols,
    }


def _merge_dm_heir_for_upload(dm_row: dict, heir_row: dict) -> dict:
    """Collapse a (DM, Heir) row pair into one row for the upload CSV.

    DataSift's upload merges incoming rows that share the same Property
    Street Address — only one survives, and the columns from the loser
    are silently discarded. The DM row carries the right contact fields
    (Owner First/Last, Mailing Address, Phone N) but a leaner TAGS
    footer; the Heir row carries the richer TAGS footer including
    filter-preset-critical tags (tier_1, in_tier, municipality_*,
    signing_chain_*, phone_dial_first, heir_of_*) and a SIGNING CHAIN
    Notes section absent from the DM row. We keep all DM contact
    columns, splice the heir's SIGNING CHAIN section into the Notes
    just above the TAGS footer, and replace the TAGS footer with the
    union of both rows' tag lists.

    Returns a new dict — neither input is mutated.
    """
    merged = dict(dm_row)

    dm_notes = dm_row.get("Notes", "") or ""
    heir_notes = heir_row.get("Notes", "") or ""

    # Split Notes on the "=== TAGS ===" marker so we can rebuild the
    # footer from the union. Everything before the marker is the
    # notes body (sections like DECEASED OWNER / DECISION MAKERS /
    # PROPERTY / PHONE TIERS). Everything after is the comma-separated
    # tag list (one line, no further sections).
    def _split_at_tags(notes: str) -> tuple[str, str]:
        marker = "=== TAGS ==="
        idx = notes.find(marker)
        if idx < 0:
            return notes, ""
        body = notes[:idx].rstrip("\n")
        tag_line = notes[idx + len(marker):].lstrip("\n").rstrip()
        return body, tag_line

    dm_body, dm_tags_line = _split_at_tags(dm_notes)
    heir_body, heir_tags_line = _split_at_tags(heir_notes)

    # Extract the SIGNING CHAIN section from the heir body and append
    # it to the DM body. The DM body already has DECEASED OWNER /
    # DECISION MAKERS / PROPERTY / PHONE TIERS — we only need to add
    # the SIGNING CHAIN piece that's exclusively on the heir row.
    signing_section = ""
    sc_marker = "=== SIGNING CHAIN"
    sc_start = heir_body.find(sc_marker)
    if sc_start >= 0:
        # Section runs until the next "=== " or end of body
        nxt = heir_body.find("\n=== ", sc_start + 1)
        signing_section = heir_body[sc_start:nxt if nxt >= 0 else len(heir_body)].rstrip()

    body_parts = [dm_body.rstrip()] if dm_body else []
    if signing_section and signing_section not in dm_body:
        body_parts.append(signing_section)
    merged_body = "\n\n".join(p for p in body_parts if p)

    # Union the two TAGS lines, preserving order, deduping.
    seen: set[str] = set()
    merged_tags: list[str] = []
    for tag_line in (dm_tags_line, heir_tags_line):
        for tag in (t.strip() for t in tag_line.split(",")):
            if tag and tag not in seen:
                seen.add(tag)
                merged_tags.append(tag)

    merged_notes = merged_body
    if merged_tags:
        merged_notes = (
            f"{merged_body}\n\n=== TAGS ===\n{','.join(merged_tags)}"
            if merged_body else f"=== TAGS ===\n{','.join(merged_tags)}"
        )

    merged["Notes"] = merged_notes
    return merged


def write_datasift_csv(
    notices: list[NoticeData],
    filename: str | None = None,
    phone_tiers: dict | None = None,
) -> Path:
    """Write notices to a DataSift-formatted CSV file.

    Args:
        notices: List of enriched NoticeData objects.
        filename: Optional filename override.
        phone_tiers: Optional Trestle scoring dict from
            ``phone_validator.score_record_phones`` — passed through to
            ``_build_row`` so phone_dial_first/etc. tags land on each row.

    Returns:
        Path to the written CSV file.
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"datasift_upload_{timestamp}.csv"

    # If caller passed a path (absolute or relative-with-dir), use it as-is.
    # If they passed a bare filename, drop it in output/leads/.
    fn_path = Path(filename)
    output_path = fn_path if fn_path.is_absolute() or fn_path.parent != Path(".") else LEADS_DIR / filename
    written = 0
    incomplete = 0
    issue_counts: dict[str, int] = {}

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()

        for notice in notices:
            row = _build_row(notice, phone_tiers=phone_tiers)
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
                logger.debug("Incomplete record %s: %s", notice.address, issues)
            writer.writerow(row)
            written += 1

    logger.info("Wrote %d records to DataSift CSV: %s", written, output_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        written - incomplete, written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", written, written)
    return output_path


def write_datasift_split_csvs(
    notices: list[NoticeData],
    date_str: str | None = None,
    phone_tiers: dict | None = None,
) -> list[dict]:
    """Generate per-distressor upload CSVs + one consolidated archive CSV.

    Architecture (operator request 2026-06-11): each distressor (code_violation,
    foreclosure, pre_probate, probate, etc.) gets its OWN upload file containing
    both the primary DM row AND every heir row for the notices of that type.
    Plus one ``datasift_archive_<ts>.csv`` with every row from every distressor
    in one file for audit / records purposes — never uploaded.

    Why per-distressor + dual-row:
      * DataSift filter is by LIST, not by tag. A property scraped as both
        foreclosure AND probate today gets a row in each upload file and lands
        on both lists. Dual-list membership IS the urgency signal.
      * Uploads can run in priority order (DISTRESSOR_PRIORITY constant) so
        probate runs LAST with Swap Owners ON without affecting earlier
        non-probate uploads of the same property.
      * Heir rows ride along on the same list as their parent DM — no
        separate Heirs CSV needed. Operator gets a clean record-per-row view
        in each list.

    Args:
        notices: Enriched NoticeData objects.
        date_str: Optional date string for filenames (default: today).
        phone_tiers: Optional Trestle scoring dict — applied to every row.

    Returns:
        List of dicts. First entry is always the archive
        ``{"role": "archive", "path": ..., ...}``. Remaining entries are
        per-distressor upload files
        ``{"role": "upload", "path": ..., "notice_type": ...,
           "list_name": ..., "priority": int, "swap_owners": bool,
           "dm_count": int, "heir_count": int}``
        sorted by priority (lowest first → upload first).
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # Pre-build every row (DM + heir variants). Each row carries its
    # notice_type so we can sort it into the right per-distressor bucket.
    #
    # We build BOTH rows as we always did so the archive CSV preserves
    # the full picture, but the upload CSV writer below collapses each
    # (DM, Heir) pair into a SINGLE merged row per notice. Reason: as
    # of 2026-06-20 the operator confirmed DataSift dedupes uploaded
    # rows by Property Address — when both DM and Heir rows landed for
    # the same property, only one survived. The surviving row had the
    # leaner DM tag footer; the richer Heir-row tags (tier_1, in_tier,
    # municipality_*, signing_chain_*, phone_dial_first, heir_of_*)
    # were silently lost, breaking every filter preset that relied on
    # them. Merging into one row preserves the full TAGS footer and
    # both notes sections in a single DataSift record.
    #
    # `pairs_by_type` keeps DM and Heir rows for the same notice
    # paired together so the upload writer can merge them deterministically.
    dm_rows_by_type: dict[str, list[dict]] = {}
    pairs_by_type: dict[str, list[tuple[dict, dict | None]]] = {}
    archive_rows: list[dict] = []  # everything, archive only — DM + heir
    incomplete = 0
    issue_counts: dict[str, int] = {}

    for notice in notices:
        nt = (notice.notice_type or "").strip() or "unknown"

        # DM row — every notice gets one
        dm_row = _build_row(
            notice,
            notes_override=_build_dm_notes(notice),
            phone_tiers=phone_tiers,
        )
        is_complete, issues = _validate_row(dm_row)
        if not is_complete:
            incomplete += 1
            for issue in issues:
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
        dm_rows_by_type.setdefault(nt, []).append(dm_row)
        archive_rows.append(dm_row)

        # Heir row — only for deceased records with heir_map_json. Stored
        # alongside its DM row in pairs_by_type for the merge step below.
        heir_row: dict | None = None
        if notice.owner_deceased == "yes" and notice.heir_map_json:
            heir_row = _build_row(
                notice,
                notes_override=_build_heir_notes(notice),
                is_heir_row=True,
                phone_tiers=phone_tiers,
            )
            archive_rows.append(heir_row)

        pairs_by_type.setdefault(nt, []).append((dm_row, heir_row))

    results: list[dict] = []

    # ── Archive CSV — all rows, never uploaded ──────────────────────
    archive_path = LEADS_DIR / f"datasift_archive_{timestamp}.csv"
    with open(archive_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for row in archive_rows:
            writer.writerow(row)
    logger.info("Archive CSV: %d rows → %s", len(archive_rows), archive_path)
    results.append({
        "role": "archive",
        "path": archive_path,
        "label": "Archive",
        "row_count": len(archive_rows),
    })

    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        len(notices) - incomplete, len(notices), incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info(
            "DataSift completeness: %d/%d clean (100%%)",
            len(notices), len(notices),
        )

    # ── Per-distressor upload CSVs ──────────────────────────────────
    # One file per notice_type that produced at least one DM row.
    # Sorted by DISTRESSOR_PRIORITY so the upload loop in daily_finalize
    # can iterate the returned list in upload order without re-sorting.
    notice_types_present = sorted(
        dm_rows_by_type.keys(),
        key=distressor_sort_key,
    )

    for nt in notice_types_present:
        list_name = NOTICE_TYPE_TO_LIST.get(nt, "")
        if not list_name:
            logger.warning(
                "No NOTICE_TYPE_TO_LIST mapping for notice_type=%r — "
                "skipping upload-file generation for %d row(s)",
                nt, len(dm_rows_by_type[nt]),
            )
            continue

        upload_path = LEADS_DIR / f"datasift_upload_{nt}_{timestamp}.csv"
        pairs = pairs_by_type.get(nt, [])

        # One row per notice: collapse (DM, Heir) pair into one merged
        # row when both exist; otherwise just write the DM row alone.
        # See _merge_dm_heir_for_upload() docstring for why.
        merged_rows: list[dict] = []
        merged_with_heir = 0
        for dm_row, heir_row in pairs:
            if heir_row is None:
                merged_rows.append(dm_row)
            else:
                merged_rows.append(_merge_dm_heir_for_upload(dm_row, heir_row))
                merged_with_heir += 1

        with open(upload_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for row in merged_rows:
                writer.writerow(row)

        swap = should_swap_owners(nt)
        priority = distressor_sort_key(nt)
        logger.info(
            "Upload CSV (%s, list=%s, prio=%d, swap=%s): "
            "%d row(s) (%d merged DM+Heir, %d DM-only) → %s",
            nt, list_name, priority, swap,
            len(merged_rows), merged_with_heir,
            len(merged_rows) - merged_with_heir, upload_path,
        )

        results.append({
            "role": "upload",
            "path": upload_path,
            "label": list_name,
            "notice_type": nt,
            "list_name": list_name,
            "priority": priority,
            "swap_owners": swap,
            "dm_count": len(merged_rows),
            "heir_count": merged_with_heir,
        })

    return results
