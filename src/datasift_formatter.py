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
    "Phone 1",
    "Phone 2",
    "Phone 3",
    "Phone 4",
    "Phone 5",
    "Phone 6",
    "Phone 7",
    "Phone 8",
    "Phone 9",
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


def _build_notes(notice: NoticeData) -> str:
    """Build a structured notes string for DataSift records.

    Deceased records get a multi-section format with heir map and DM summary.
    Living records get a simpler single-section format.
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

        # Section 2: Decision makers
        dm_section = _build_dm_section(notice)
        if dm_section:
            sections.append(dm_section)

        # Section 3: Heir map
        heir_section = _build_heir_summary(notice)
        if heir_section:
            sections.append(heir_section)

        # Section 4: Property/notice details
        prop_section = _build_property_section(notice)
        if prop_section:
            sections.append(f"=== PROPERTY ===\n{prop_section}")

        if notice.report_url:
            sections.append(f"=== REPORT ===\n{notice.report_url}")

        return "\n\n".join(sections)

    # Living owner — simple format
    return _build_property_section(notice)


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
    """Build Notes for CSV 2: full heir map only.

    Used by write_datasift_split_csvs() for the Heirs upload.
    Returns empty string if no heir data.
    """
    return _build_heir_summary(notice)


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
    tags = _build_tags(notice, phone_tiers=phone_tiers)
    if is_heir_row and notice.notice_type:
        heir_tag = f"heir_of_{notice.notice_type}"
        tags = f"{tags},{heir_tag}" if tags else heir_tag
    list_name = NOTICE_TYPE_TO_LIST.get(notice.notice_type, "")
    notes = notes_override if notes_override is not None else _build_notes(notice)

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

    return {
        # ── Core auto-mapped ──
        "Property Street Address": notice.address,
        "Property City": notice.city,
        "Property State": notice.state or "AL",
        "Property ZIP Code": notice.zip,
        "Owner First Name": contact["first"],
        "Owner Last Name": contact["last"],
        "Mailing Street Address": contact["street"],
        "Mailing City": contact["city"],
        "Mailing State": contact["state"],
        "Mailing ZIP Code": contact["zip"],
        # ── Phone/Email (Tracerfy → DataSift generic Phone N format) ──
        "Phone 1": notice.primary_phone,
        "Phone 2": notice.mobile_1,
        "Phone 3": notice.mobile_2,
        "Phone 4": notice.mobile_3,
        "Phone 5": notice.mobile_4,
        "Phone 6": notice.mobile_5,
        "Phone 7": notice.landline_1,
        "Phone 8": notice.landline_2,
        "Phone 9": notice.landline_3,
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
    }


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
    """Generate separate DM and Heir Map CSVs for two-upload Message Board flow.

    CSV 1 ("DMs"): All records. Deceased get DM breakdown as Notes, living get
    property details. Creates/updates all records in DataSift.

    CSV 2 ("Heirs"): Only deceased records with heir data. Notes = full heir map.
    DataSift merges by address, adding a second Message Board comment.

    Args:
        notices: List of enriched NoticeData objects.
        date_str: Optional date string for filenames/list names (default: today).
        phone_tiers: Optional Trestle scoring dict — applied to both DMs and
            Heirs rows so phone_dial_first/etc. tags appear on every row.

    Returns:
        List of dicts: [{"path": Path, "label": str, "list_name": str}, ...]
        Returns 1 item if no deceased-with-heirs, 2 items otherwise.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results = []

    # CSV 1: DMs — all records (lands in output/leads/)
    dm_path = LEADS_DIR / f"datasift_upload_DMs_{timestamp}.csv"
    dm_written = 0
    incomplete = 0
    issue_counts: dict[str, int] = {}
    with open(dm_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for notice in notices:
            row = _build_row(
                notice,
                notes_override=_build_dm_notes(notice),
                phone_tiers=phone_tiers,
            )
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
            writer.writerow(row)
            dm_written += 1

    logger.info("DMs CSV: %d records → %s", dm_written, dm_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        dm_written - incomplete, dm_written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", dm_written, dm_written)
    results.append({
        "path": dm_path,
        "label": "DMs",
        "list_name": f"SiftStack {date_str} - DMs",
    })

    # CSV 2: Heirs — only deceased with heir data
    deceased_with_heirs = [
        n for n in notices
        if n.owner_deceased == "yes" and n.heir_map_json
    ]

    if deceased_with_heirs:
        heir_path = LEADS_DIR / f"datasift_upload_Heirs_{timestamp}.csv"
        heir_written = 0
        with open(heir_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for notice in deceased_with_heirs:
                row = _build_row(
                    notice,
                    notes_override=_build_heir_notes(notice),
                    is_heir_row=True,
                    phone_tiers=phone_tiers,
                )
                writer.writerow(row)
                heir_written += 1

        logger.info("Heirs CSV: %d records → %s", heir_written, heir_path)
        results.append({
            "path": heir_path,
            "label": "Heirs",
            "list_name": f"SiftStack {date_str} - Heirs",
        })
    else:
        logger.info("No deceased records with heir data — skipping Heirs CSV")

    return results
