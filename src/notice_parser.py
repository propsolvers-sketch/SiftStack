"""Parse individual notice pages and extract structured data.

After reCAPTCHA is solved and "View Notice" is clicked, the detail page shows:
  1. Structured metadata labels: Publication Name, Publication City and State,
     Publication County, Notice Publish Date
  2. A "Notice Content" section with the raw legal text body

We extract the metadata labels directly, then regex-parse address/owner/etc.
from the Notice Content body.

IMPORTANT: For address parsing, we ONLY extract addresses that appear after
high-confidence property-indicator phrases like "commonly known as" or
"property address". We never fall back to a generic address regex — it's better
to leave the address empty than to grab a courthouse, auction location, or
instrument number by mistake.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from playwright.async_api import Page

logger = logging.getLogger(__name__)


@dataclass
class NoticeData:
    """Structured data extracted from a single notice."""
    date_added: str = ""       # Published date (YYYY-MM-DD)
    auction_date: str = ""     # Scheduled sale/auction date (YYYY-MM-DD)
    address: str = ""
    city: str = ""
    state: str = "AL"
    zip: str = ""
    owner_name: str = ""
    owner_first_name: str = ""  # Split from owner_name (first token)
    owner_middle_name: str = "" # Split from owner_name (middle tokens, joined)
    owner_last_name: str = ""   # Split from owner_name (last token, ignoring suffixes)
    owner_suffix: str = ""      # Generational suffix (Jr/Sr/II/III/IV)
    notice_type: str = ""      # foreclosure | tax_sale | tax_lien | probate
    # Foreclosure-specific party metadata (populated by regex + LLM fallback)
    mortgage_company: str = ""        # Current Mortgagee/Transferee (e.g. "Nationstar Mortgage LLC")
    original_lender: str = ""         # Original mortgagee at execution (often MERS as nominee for X)
    trustee: str = ""                 # Law firm / trustee conducting sale (e.g. "Tiffany & Bosco, P.A.")
    trustee_file_number: str = ""     # Trustee's internal file # (e.g. "25-40447-WF-AL")
    county: str = ""
    source_url: str = ""
    raw_text: str = ""         # Full notice text for classification
    # Smarty address standardization fields (populated post-scrape)
    zip_plus4: str = ""        # Full ZIP+4 (e.g. "37918-1234")
    latitude: str = ""         # Decimal latitude from Smarty geocode
    longitude: str = ""        # Decimal longitude from Smarty geocode
    dpv_match_code: str = ""   # Delivery Point Validation: Y=confirmed, S=secondary missing, N=no match
    vacant: str = ""           # "Y" if address is vacant
    rdi: str = ""              # "Residential" or "Commercial"
    # Zillow property enrichment fields (populated post-scrape)
    mls_status: str = ""           # "Active", "Pending", "Sold", "Off Market"
    mls_listing_price: str = ""    # Current list price or last sold price
    mls_last_sold_date: str = ""   # Most recent sale date (YYYY-MM-DD)
    mls_last_sold_price: str = ""  # Most recent sale price
    estimated_value: str = ""      # Zestimate
    estimated_equity: str = ""     # zestimate - estimated remaining mortgage
    equity_percent: str = ""       # (equity / zestimate) * 100
    property_type: str = ""        # "Single Family", "Condo", etc.
    bedrooms: str = ""
    bathrooms: str = ""
    sqft: str = ""
    year_built: str = ""
    lot_size: str = ""             # Lot size in sqft
    # Probate-specific fields
    decedent_name: str = ""        # Deceased person's name (probate only)
    decedent_first_name: str = ""  # Split from decedent_name
    decedent_middle_name: str = "" # Split from decedent_name (middle tokens, joined)
    decedent_last_name: str = ""   # Split from decedent_name
    decedent_suffix: str = ""      # Generational suffix (Jr/Sr/II/III/IV)
    owner_street: str = ""         # PR/contact mailing street address
    owner_city: str = ""           # PR/contact mailing city
    owner_state: str = ""          # PR/contact mailing state
    owner_zip: str = ""            # PR/contact mailing zip
    case_number: str = ""          # Probate case # (e.g. "PC2025-234", "PR-2026-000557")
    judge_name: str = ""           # Judge of Probate (e.g. "Tammy Brown", "James P. Naftel")
    granted_date: str = ""         # Date Letters Testamentary/Administration granted (YYYY-MM-DD); starts the AL 6-month creditor clock
    creditor_deadline: str = ""    # granted_date + 6 months (YYYY-MM-DD)
    # Probate notice subtype + sale-specific fields (Tweak #3)
    notice_subtype: str = ""           # "probate_creditors" | "probate_sale" | "probate_heirs_notice"
    petition_filed_date: str = ""      # YYYY-MM-DD; when the PR petitioned to sell (probate_sale only)
    hearing_date: str = ""             # YYYY-MM-DD; court ruling date for probate_sale petitions
    co_pr_names: str = ""              # Pipe-delimited co-Personal Representatives
    heirs_named_in_notice: str = ""    # Pipe-delimited heirs explicitly named (probate_heirs_notice)
    estate_purpose: str = ""           # e.g. "paying the debts of the said estate"
    sale_type: str = ""                # "private" | "public auction"
    # Multi-parcel + homestead enrichment (Tweak #1)
    secondary_addresses: str = ""      # Pipe-delimited additional parcels owned by decedent
    total_estate_value: str = ""       # Sum of all parcel total_value (primary + secondary)
    is_homestead: str = ""             # "Y" if the matched parcel appears to be the primary residence
    # Final-pass column coverage
    received_date: str = ""            # When the notice was scraped (UTC, YYYY-MM-DD)
    assessed_value: str = ""           # Last-assessed value of the PRIMARY parcel only
    property_use: str = ""             # Assessor classification ("Residential", "Commercial", "Real", etc.)
    survivor_zip: str = ""             # Zip of the first surviving heir (when distinct from PR)
    municipality: str = ""             # Assessor's municipality code (Jefferson DispCode: BHAM, TRUSSVILLE, COUNTY, etc.)
    # County assessor / tax fields
    parcel_id: str = ""                # County assessor parcel ID
    tax_delinquent_amount: str = ""    # Total delinquent tax owed ($)
    tax_delinquent_years: str = ""     # Number of years delinquent
    # Deceased owner detection
    deceased_indicator: str = ""       # "life_estate", "personal_rep", "trustee", "care_of", "et_al", or ""
    tax_owner_name: str = ""           # Raw owner name from county tax API
    # Obituary-confirmed deceased owner
    owner_deceased: str = ""                # "yes" or "" — confirmed via obituary search
    date_of_death: str = ""                 # YYYY-MM-DD from obituary
    obituary_url: str = ""                  # URL of confirmed obituary
    decision_maker_name: str = ""           # Heir/executor full name
    decision_maker_relationship: str = ""   # "spouse", "son", "daughter", "executor", etc.
    # Deep prospecting — ranked decision-makers (flat columns)
    decision_maker_status: str = ""         # "verified_living", "unverified"
    decision_maker_source: str = ""         # "obituary_survivors", "tax_record_joint_owner", "snippet"
    decision_maker_street: str = ""         # DM residential mailing address
    decision_maker_city: str = ""
    decision_maker_state: str = ""
    decision_maker_zip: str = ""
    decision_maker_2_name: str = ""
    decision_maker_2_relationship: str = ""
    decision_maker_2_status: str = ""       # "verified_living", "unverified"
    decision_maker_3_name: str = ""
    decision_maker_3_relationship: str = ""
    decision_maker_3_status: str = ""       # "verified_living", "unverified"
    # Obituary/heir metadata
    obituary_source_type: str = ""          # "full_page" or "snippet"
    heir_search_depth: str = ""             # "0" (none), "1" (survivors checked), "2" (2nd gen)
    heirs_verified_living: str = ""         # Count of verified living heirs
    heirs_verified_deceased: str = ""       # Count of verified deceased heirs
    heirs_unverified: str = ""              # Count of unverified heirs
    heir_map_json: str = ""                 # JSON-encoded full ranked heir list (all heirs, not just top 3)
    signing_chain_count: str = ""            # Count of living signing-authority heirs
    signing_chain_names: str = ""            # Comma-separated names of signing-authority heirs
    # Error map (flat fields)
    dm_confidence: str = ""                 # "high", "medium", "low"
    dm_confidence_reason: str = ""          # Brief explanation
    missing_data_flags: str = ""            # Pipe-separated: "no_survivors|snippet_only|common_name"
    # Provenance for decision_maker_street/city/state/zip — added 2026-06-21
    # so operator can tell apart real DM mailing addresses from
    # property-fallback placeholders. Values:
    #   ""                   — no DM mailing set
    #   "tracerfy"           — from Tracerfy batch skip-trace
    #   "jefferson_tax_api"  — from Jefferson E-Ring tax-roll mailing fields
    #   "madison_tax_api"    — future: from Madison per-parcel detail
    #   "marshall_tax_api"   — future: from Marshall per-parcel detail
    #   "people_search"      — from Serper+Firecrawl+LLM (Tier 2)
    #   "ddg_people_search"  — from DuckDuckGo+LLM (Tier 2b)
    #   "property_fallback"  — copied from property address (safety net)
    # The formatter emits a `mailing_unverified` tag for property_fallback
    # so direct-mail filter presets can de-prioritize placeholder rows.
    dm_mailing_source: str = ""
    # Mailability flag
    mailable: str = ""                 # "yes" or "" (unmailable)
    # Entity research fields
    entity_type: str = ""                  # "llc", "corp", "trust", "estate", "lp", "other"
    entity_person_name: str = ""           # Person found behind entity (full name)
    entity_person_role: str = ""           # "registered_agent", "member", "trustee", "officer", etc.
    entity_research_source: str = ""       # "name_parse", "web_search", "sos_snippet"
    entity_research_confidence: str = ""   # "high", "medium", "low"
    # PDF report link (Google Drive URL, populated by report_generator)
    report_url: str = ""
    # Tracerfy skip trace — phones + emails (populated by tracerfy_skip_tracer)
    primary_phone: str = ""
    mobile_1: str = ""
    mobile_2: str = ""
    mobile_3: str = ""
    mobile_4: str = ""
    mobile_5: str = ""
    landline_1: str = ""
    landline_2: str = ""
    landline_3: str = ""
    email_1: str = ""
    email_2: str = ""
    email_3: str = ""
    email_4: str = ""
    email_5: str = ""
    # Pipeline metadata (set by enrichment_pipeline)
    run_id: str = ""                   # Unique pipeline run identifier for data lineage


# ── Known TN cities in Knox & Blount counties ─────────────────────────
# Sorted longest-first so "Lenoir City" matches before "City"
TN_CITIES: list[str] = sorted(
    [
        "Knoxville", "Maryville", "Alcoa", "Farragut", "Powell",
        "Lenoir City", "Loudon", "Oak Ridge", "Clinton", "Sevierville",
        "Pigeon Forge", "Gatlinburg", "Karns", "Halls", "Concord",
        "Friendsville", "Louisville", "Townsend", "Walland", "Rockford",
        "Corryton", "Mascot", "Strawberry Plains", "New Market",
        "Kodak", "Dandridge", "Bean Station", "Jefferson City",
        "Morristown", "Madisonville", "Vonore", "Greenback",
    ],
    key=len,
    reverse=True,
)

# ── Known AL cities in Jefferson & Madison counties ────────────────────
AL_CITIES: list[str] = sorted(
    [
        # Jefferson County (Birmingham metro)
        "Birmingham", "Bessemer", "Hoover", "Vestavia Hills", "Homewood",
        "Mountain Brook", "Trussville", "Center Point", "Fultondale",
        "Gardendale", "Pleasant Grove", "Tarrant", "Irondale", "Leeds",
        "Pinson", "Adamsville", "Forestdale", "Fairfield", "Midfield",
        "Brighton", "Lipscomb", "Warrior", "Clay", "Graysville",
        # Madison County (Huntsville metro)
        "Huntsville", "Madison", "New Hope", "Owens Cross Roads",
        "Triana", "Gurley", "Hazel Green", "Meridianville", "Harvest",
        "Toney", "Brownsboro", "Ryland",
    ],
    key=len,
    reverse=True,
)

# Set version for O(1) membership tests in standalone address validation
# Combined TN + AL — validation only checks if a token *is* a known city
_KNOWN_CITIES_SET: set[str] = {c.title() for c in TN_CITIES} | {c.title() for c in AL_CITIES}

# ── Reusable suffix pattern ──────────────────────────────────────────
# Word-boundary at the end prevents matching "Cir" inside "Circuit", etc.
_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|"
    r"Pike|Highway|Hwy|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
    r"Cove|Cv|Loop|Run|Path|Ridge|Rdg|Crossing|Xing|"
    r"Bend|Point|Pt|Pass|Hollow|Holw|Glen|Glenn|View|"
    r"Landing|Lndg|Row|Trace|Walk|Knoll|Overlook|Crest|Spur|Commons)\b"
)

# House number (1-5 digits) + optional direction + street words + suffix
# Uses (?:\w+\s+)+? to match one or more street name words (each followed by
# a space), then the suffix. This is much safer than [\w\s]+? which grabs junk.
_ADDR_PART = (
    r"(\d{1,5}\s+"                 # house number
    r"(?:[NSEW]\.?\s+)?"           # optional direction prefix
    r"(?:[\w'-]+\s+)+?"            # street name words (1+)
    + _SUFFIX +
    r"\.?"                         # optional trailing period
    r"(?:\s+(?:NE|NW|SE|SW|N|S|E|W))?"  # optional trailing directional (e.g. "Dr SW")
    r")"
)

# ── Property address indicator phrases (high confidence) ─────────────
# These phrases appear in legal notices right before the actual property address.
_PROP_INDICATOR = (
    r"(?:"
    r"commonly\s+known\s+as"
    r"|property\s+known\s+as"
    r"|property\s+address\s*(?:is|of|:)"
    # Alabama foreclosure-notice convention (Jefferson + Madison Counties)
    r"|property\s+street\s+address\s+for\s+informational\s+purposes(?:\s+only)?\s*[:.]?"
    r"|(?:real\s+)?property\s+(?:located|situated)\s+at"
    r"|said\s+property\s+(?:being|is)"
    r"|hereinafter\s+(?:known|described)\s+as"
    r"|also\s+known\s+as"
    r"|a/?k/?a"
    r"|known\s+as"
    r"|bearing\s+the\s+address\s+(?:of\s+)?"
    r"|having\s+(?:the\s+)?address\s+(?:of\s+)?"
    r"|street\s+address\s*(?:is|of|:)?"
    r"|civic\s+address\s*(?:is|of|:)?"
    r"|property\s+at"
    r"|being\s+the\s+(?:same\s+)?property\s+(?:located\s+)?at"
    r"|with\s+(?:the|an)\s+address\s+(?:of\s+)?"
    r"|the\s+address\s+of\s+(?:which|said\s+property|the\s+property)\s+(?:is|being)"
    r"|referred\s+to\s+as"
    r"|(?:property\s+)?identified\s+as"
    r"|address/?description\s*:"
    r")"
)

# Optional ", Knox County" or ", Blount County" between city and state
_OPTIONAL_COUNTY = r"(?:\s*[,.]\s*\w+\s+County)?"

# FULL match: indicator + address + city + [county] + Tennessee/TN + zip
# Captures (address, city, zip) all from the same context.
FULL_PROPERTY_RE = re.compile(
    _PROP_INDICATOR
    + r"\s*[:.,\s]*"
    + _ADDR_PART
    + r"(?:\s*[,.]?\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?"
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"           # city name
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + r"(?:Tennessee|Tenn\.?|TN|Alabama|Ala\.?|AL)"
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",     # optional zip
    re.IGNORECASE,
)

# Address-only match: indicator + address (no city/state/zip in same line)
PROPERTY_ADDR_RE = re.compile(
    _PROP_INDICATOR + r"\s*[:.,\s]*" + _ADDR_PART,
    re.IGNORECASE,
)

# "located at ADDRESS, CITY, TN ZIP" — secondary, used for tax sales
# We validate the result against the blacklist to filter auction locations.
LOCATED_AT_FULL_RE = re.compile(
    r"located\s+at\s+"
    + _ADDR_PART
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + r"(?:Tennessee|Tenn\.?|TN|Alabama|Ala\.?|AL)"
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)

LOCATED_AT_ADDR_RE = re.compile(
    r"located\s+at\s+" + _ADDR_PART,
    re.IGNORECASE,
)

# Standalone "ADDRESS, CITY, TN ZIP" — no indicator phrase required.
# Only used for tax_sale / tax_lien notices as a last resort before giving up.
STANDALONE_ADDR_RE = re.compile(
    _ADDR_PART
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"           # city name
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + r"(?:Tennessee|Tenn\.?|TN|Alabama|Ala\.?|AL)"
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",     # optional zip
    re.IGNORECASE,
)

# ── Address validation ───────────────────────────────────────────────

# Words that indicate the address is a courthouse / auction location / office
_BAD_ADDR_WORDS = [
    "courthouse", "court house", "county building", "city building",
    "city county", "register", "office of", "entrance",
    "county court", "usual and customary", "main entrance",
]

# Known government / courthouse addresses (normalized lowercase)
_KNOWN_BAD_ADDRS = [
    "400 main street",      # Knox County City-County Building
    "400 main avenue",
    "400 main ave",
    "400 w main",
    "345 court street",     # Blount County courthouse area
    "345 court st",
    "800 s gay st",         # Downtown Knoxville (law offices)
    "800 s. gay st",
    "800 south gay",
    "300 main street",      # Blount County courthouse
    "300 main st",
]


def _is_valid_address(addr: str) -> bool:
    """Reject addresses that are clearly not property addresses."""
    if not addr or len(addr.strip()) < 5:
        return False

    lower = addr.lower()

    # Reject if contains courthouse/office keywords
    for bad in _BAD_ADDR_WORDS:
        if bad in lower:
            return False

    # Reject if matches known government/courthouse addresses
    normalized = re.sub(r"\s+", " ", lower.strip())
    for bad_addr in _KNOWN_BAD_ADDRS:
        if normalized.startswith(bad_addr):
            return False

    # House number sanity: must be 1-99999
    m = re.match(r"(\d+)", addr)
    if m:
        num = int(m.group(1))
        if num < 1 or num > 99999:
            return False

    return True


# ── TN zip code ──────────────────────────────────────────────────────
# TN zips range from 37010 to 38589 — require 37xxx or 38xxx prefix
ZIP_RE = re.compile(r"\b(3[78]\d{3})(?:-\d{4})?\b")

# Zips to reject when found via fallback (no address context):
# Courthouse / auction / law-office zips that commonly appear in notice text
_COURTHOUSE_ZIPS = {
    "37902",  # Downtown Knoxville (courthouse, City-County Building)
    "37901",  # Knoxville PO Box area
    "38103",  # Memphis (law firms often referenced)
    "38101",  # Memphis PO Box area
    "37219",  # Nashville (state offices)
}

# Expected zip prefixes by county (for fallback validation)
_COUNTY_ZIP_PREFIXES: dict[str, list[str]] = {
    "Knox":   ["377", "378", "379"],
    "Blount": ["377", "378"],
}


# ── Owner name patterns ──────────────────────────────────────────────

# "executed by JOHN DOE AND JANE DOE, conveying..."
# Stop words expanded to catch "conveying", "wife", "husband", etc.
EXECUTED_BY_RE = re.compile(
    r"executed\s+(?:on\s+\w+\s+\d+,?\s+\d{4},?\s+)?by\s+"
    r"([A-Z][A-Za-z\s.,]+?)"
    r"(?:"
    r"\s*,\s*(?:conveying|a\s|an\s|as\s|her\s|his\s|to\s|who\s|wife|husband|"
    r"being|unmarried|single|granting|transferring|said|the\s|for\s+the\s+benefit)"
    r"|\s+conveying\b"
    r"|\s+granting\b"
    r"|\s+transferring\b"
    r"|\s+for\s+the\s+benefit\b"
    r"|\s+to\s+[\w\s,]+?(?:trustee|trust\b)"
    r"|\s*\("
    r"|\.\s+(?:The|Said|This|Such)"
    r")",
    re.IGNORECASE,
)

# "made by NAME" / "given by NAME" — common in deed of trust references
MADE_BY_RE = re.compile(
    r"(?:made|given)\s+by\s+"
    r"([A-Z][A-Za-z\s.,]+?)"
    r"(?:\s*,\s*(?:dated|to\s|conveying|a\s|an\s|as\s|her\s|his\s|who\s|wife|husband|"
    r"being|unmarried|single|granting|transferring|said|the\s)"
    r"|\s+(?:dated|to\s+[\w\s,]+?(?:trustee|trust\b))"
    r"|\s*\("
    r"|\.\s+(?:The|Said|This|Such))",
    re.IGNORECASE,
)

# "from NAME to TRUSTEE" — deed of trust transfer language
FROM_TO_RE = re.compile(
    r"from\s+([A-Z][A-Za-z\s.,]+?)\s*,?\s+to\s+[\w\s,]+?(?:trustee|trust\b)",
    re.IGNORECASE,
)

# "Grantor(s): NAME" / "the grantor, NAME"
GRANTOR_RE = re.compile(
    r"grantor\(?s?\)?\s*(?:herein)?[:\s,]+([A-Z][A-Za-z\s.,]+?)"
    r"(?:\s*,\s*(?:conveying|to\s|a\s|an\s|dated)|"
    r"\s+to\s+[\w\s,]+?(?:trustee|trust\b)|"
    r"\s*\(|\.\s+)",
    re.IGNORECASE,
)

# "borrower(s): NAME" / "the borrower, NAME"
BORROWER_RE = re.compile(
    r"borrower\(?s?\)?\s*[,:\s]+(?:being\s+)?([A-Z][A-Za-z\s.,]+?)"
    r"(?:\s*,|\s+at\b|\s+of\b|\s+in\b|\s*\(|\.\s+)",
    re.IGNORECASE,
)

# "WHEREAS, NAME, as borrower(s), executed" — Vylla/Brock & Scott format
WHEREAS_BORROWER_RE = re.compile(
    r"WHEREAS,\s+([A-Z][A-Za-z\s.,]+?)"
    r"\s*,\s*(?:as\s+borrower|an?\s+unmarried|husband\s+and\s+wife|"
    r"a\s+(?:single|married)|wife\s+and\s+husband)",
    re.IGNORECASE,
)

# "Whereas, NAME by Deed of Trust" / "NAME executed a Deed of Trust" — Nestor format
WHEREAS_DEED_RE = re.compile(
    r"WHEREAS,\s+([A-Z][A-Za-z\s.,]+?)\s+(?:by\s+Deed|executed\s+a\s+Deed)",
    re.IGNORECASE,
)

# "Current Owner(s): NAME" — structured label in some notice formats
CURRENT_OWNER_RE = re.compile(
    r"Current\s+Owner\(?s?\)?\s*:\s*([A-Z][A-Za-z\s.,]+?)(?:\s*\n|\s*$|\s+Other)",
    re.IGNORECASE | re.MULTILINE,
)

# Fallback owner patterns
OWNER_PATTERNS = [
    MADE_BY_RE,
    FROM_TO_RE,
    GRANTOR_RE,
    BORROWER_RE,
    WHEREAS_BORROWER_RE,
    WHEREAS_DEED_RE,
    CURRENT_OWNER_RE,
    re.compile(r"default\s+(?:of|by)\s+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s*\(|\s+in\b)", re.IGNORECASE),
    re.compile(r"property\s+of\s+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s*\(|\s+in\b)", re.IGNORECASE),
    re.compile(r"against\s+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s+for\b|\s+at\b)", re.IGNORECASE),
]

# Probate — personal representative / executor / administrator
PROBATE_NAME_RE = re.compile(
    r"(?:Personal\s+Representative(?:\(S\))?|Executor|Executrix|Administrator|Administratrix)"
    r"[:\s]+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s*\(|\s+of\b|\s+for\b|\s+\d|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# AL Pattern A — "having been granted to NAME on/as..." — name appears between
# the action verb and a date/role marker. Most reliable for AL probate prose.
PROBATE_NAME_GRANTED_RE = re.compile(
    r"(?:having\s+been\s+)?granted\s+to\s+"
    r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,4})"
    r"\s+(?:on\s+(?:the\s+)?\d|as\s+(?:Personal|Executor|Executrix|Administrator|Administratrix)|by\s+the\s+(?:Hon|Honorable))",
    re.IGNORECASE,
)

# AL Pattern B — "NAME\nPersonal Representative" or "NAME, Personal Representative"
# Common in the signature block at the end of AL notices.
PROBATE_NAME_BEFORE_TITLE_RE = re.compile(
    r"(?:^|\n)\s*"
    r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,4})"
    r"\s*[\n,]\s*"
    r"(?:Personal\s+Representative|Executor|Executrix|Administrator|Administratrix)",
    re.IGNORECASE | re.MULTILINE,
)

# Probate — decedent name from "Estate of [NAME], Deceased"
DECEDENT_NAME_RE = re.compile(
    r"Estate\s+of\s+([A-Z][A-Za-z\s.,'\-]+?)"
    r"(?:\s*,?\s*(?:Deceased|Dec['\u2019.]?\s*d|who\s+died))",
    re.IGNORECASE,
)

# ── AL probate metadata patterns ──────────────────────────────────────
# Case number: "Case No. PC2025-234", "CASE NO: PR-2026-000557", "Case# PC2025-234"
CASE_NUMBER_RE = re.compile(
    r"(?:Case\s*(?:No\.?|Number|#)|CASE\s+NO\.?)\s*[:.]?\s*"
    r"([A-Z]{2,4}[-\s]?\d{4}[-\s]?\d{1,8}|\d{4}[-\s]\d{1,8})",
    re.IGNORECASE,
)

# Judge of Probate — "by the Honorable Tammy Brown, Judge of Probate" or "Hon. James P. Naftel".
JUDGE_RE = re.compile(
    r"(?:by\s+the\s+)?(?:Honorable|Hon\.?)\s+"
    r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})"
    r"(?:\s*,?\s*Judge\s+of\s+Probate)?",
    re.IGNORECASE,
)

# Granted date — handles "the 17 day of April, 2026" and "April 7, 2026" formats.
GRANTED_DATE_RE = re.compile(
    r"(?:Letters\s+(?:Testamentary|of\s+Administration)|having\s+been\s+granted)\b"
    r"[\s\S]{0,200}?"
    r"\bon\s+(?:the\s+)?"
    r"(?:(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s*,?\s*(\d{4})"
    r"|"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})\s*,?\s*(\d{4})"
    r")",
    re.IGNORECASE,
)

# Recording stamp date at the top of AL probate filings — most reliable when
# OCR garbles the prose. Format: "MM/DD/YYYY HH:MMam/pm".
RECORDING_DATE_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{4})\s*\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?",
    re.IGNORECASE,
)

# ── Probate notice-subtype detection (date-pattern definitions follow below) ──
# Three subtypes seen in AL APN probate notices:
#   probate_creditors    — Notice to Creditors (the standard 6-month publication)
#   probate_sale         — PR petitioning the court for permission to sell real estate
#   probate_heirs_notice — Notice TO heirs about an estate proceeding (heirs named inline)
PROBATE_SALE_SIGNATURE_RE = re.compile(
    r"(?:NOTICE\s+OF\s+(?:PETITION\s+TO\s+APPROVE\s+)?SALE\s+OF\s+REAL\s+(?:PROPERTY|ESTATE)"
    r"|PETITION\s+TO\s+(?:APPROVE\s+)?SALE\s+OF\s+REAL\s+PROPERTY"
    r"|NOTICE\s+OF\s+SALE\s+OF\s+REAL\s+ESTATE\s+BY\s+(?:THE\s+)?PERSONAL\s+REPRESENTATIVE)",
    re.IGNORECASE,
)
PROBATE_HEIRS_NOTICE_RE = re.compile(
    r"NOTICE\s+TO\s*:\s*"
    r"((?:[A-Z][A-Z.\s\-']+,?\s*){2,})",  # 2+ comma-separated all-caps names
    re.IGNORECASE,
)

# "by Co-Personal Representatives of the Estate of NAME" — captures the wrapper
# so we know there are multiple PRs; the names themselves come from PROBATE_NAME_*
CO_PR_FLAG_RE = re.compile(
    r"\bCo[-\s]?(?:Personal\s+Representatives?|Executors?|Administrators?|Executrices)\b",
    re.IGNORECASE,
)

# "for purpose of paying the debts of the said estate"
ESTATE_PURPOSE_RE = re.compile(
    r"for\s+(?:the\s+)?purposes?\s+of\s+([\w\s,'\-]+?)(?:\s*\.|\s*Notice|\s*$)",
    re.IGNORECASE,
)

# Sale type — "private sale" or "public auction"
SALE_TYPE_RE = re.compile(
    r"\b(private\s+sale|public\s+(?:auction|sale))\b",
    re.IGNORECASE,
)

# PETITION_FILED_RE and HEARING_DATE_RE depend on _DATE_FRAGMENT, defined below;
# they are constructed lazily after _DATE_FRAGMENT is in scope.
PETITION_FILED_RE = None  # populated below
HEARING_DATE_RE = None    # populated below


# Probate — PR mailing address (street + city + TN + zip after the PR title)
# Anchors from the PR title keyword, skips over name/title (non-digit chars),
# then captures: (1) street address, (2) city, (3) zip
PR_ADDRESS_RE = re.compile(
    r"(?:Personal\s+Representative(?:\(S\))?|Executor|Executrix|Administrator|Administratrix)"
    r"[^0-9]{3,80}"                   # skip PR name + optional title suffix
    r"(\d{1,5}\s+"                     # house number
    r"[\w\s.,'#-]+?"                   # street name words (non-greedy)
    + _SUFFIX +
    r"\.?"
    r"(?:\s*[,.]?\s*(?:Suite|Ste\.?|Apt\.?|Unit|#)\s*\.?\s*[\w.]+)?)"  # optional unit
    r"\s*[,.\s]+\s*"
    r"([A-Za-z][\w\s]*?)"             # city
    r"\s*[,.]\s*"
    r"(?:Tennessee|Tenn\.?|TN|Alabama|Ala\.?|AL)"
    r"\s*[,.\s]*"
    r"(\d{5})",                        # zip
    re.IGNORECASE,
)

# AL signature-block PR mailing address: name comes BEFORE title, address
# follows title with only a newline separator (typically). PR_ADDRESS_RE
# above requires {3,80} non-digit chars between the title and the address,
# which fits the TN inline format ("Personal Representative: NAME, ADDR")
# but never matches the AL vertical signature block:
#
#     JOHN SMITH
#     Personal Representative
#     123 Main St
#     Birmingham, AL 35203
#
# Here, between "Representative" and "123" there is only a single newline
# (1 char) — well under the 3-char minimum. This variant uses {0,80}?
# (non-greedy zero minimum) so the address can follow the title with
# minimal whitespace, and accepts both AL and TN state-name suffixes so
# either format flows through this single fallback.
PR_ADDRESS_NAME_FIRST_RE = re.compile(
    r"(?:Personal\s+Representative(?:\(S\))?|Executor|Executrix|Administrator|Administratrix)"
    r"[^0-9]{0,80}?"                  # AL: zero+ chars (often just a newline)
    r"(\d{1,5}\s+"
    r"[\w\s.,'#-]+?"
    + _SUFFIX +
    r"\.?"
    r"(?:\s*[,.]?\s*(?:Suite|Ste\.?|Apt\.?|Unit|#)\s*\.?\s*[\w.]+)?)"
    r"\s*[,.\s]+\s*"
    r"([A-Za-z][\w\s]*?)"
    r"\s*[,.]\s*"
    r"(?:Alabama|Ala\.?|AL|Tennessee|Tenn\.?|TN)"
    r"\s*[,.\s]*"
    r"(\d{5})",
    re.IGNORECASE,
)

# Names that are clearly not real person names
_INVALID_NAMES = {
    "said property", "the grantor", "the grantors", "the creditor",
    "the creditors", "the respondent", "respondent", "the defendant",
    "defendant", "the borrower", "the mortgagor", "the debtor",
    "the estate", "the above", "the property", "the court",
    "all persons", "unknown heirs", "you in the", "you and",
    "the cause", "the following", "the undersigned",
    "executed a deed", "executed a d", "default having",
    # AL probate-notice boilerplate the LLM/regex sometimes captures as a
    # PR/owner name. These are role descriptors or sentence fragments,
    # never real person names.
    "for the estate", "of the estate", "of the state",
    "under the will", "of the will",
    "personal representative", "executor of",
    "administrator of", "executrix of", "administratrix of",
    "co-personal representative", "barred", "having been",
    "to be barred", "letters testamentary",
    # P2 #8: live-observed leaks — these phrases were captured as PR
    # names in 2026-05-13 main.py daily run (Embril Dale Edwards →
    # "Of The Will"; Willie James Fitts → "A Protected Person, Now";
    # Linda Gay Fullman → "A.K.A. Linda G. Fullman").
    "a protected person", "an incapacitated person", "an adult",
    "a/k/a", "a.k.a.", "aka",
}


# P2 #8: legal-qualifier phrases that get appended to decedent names in
# AL probate notices and must be stripped before the property locator runs.
# Examples observed in 2026-05-13 main.py daily:
#   "Jean Sutliffe, A Protected Person, Now Deceased"
#       → cleaned to "Jean Sutliffe"
#   "Linda Gay Fullman, A.K.A. Linda G. Fullman, Deceased"
#       → cleaned to "Linda Gay Fullman"
_DECEDENT_NAME_SUFFIX_RE = re.compile(
    r",\s*(?:"
    r"a\s+protected\s+person"
    r"|an?\s+incapacitated\s+person"
    r"|an?\s+adult"
    r"|a[./]?k[./]?a[.\s]"
    r"|aka\s"
    r"|now"
    r"|deceased"
    r").*$",
    re.IGNORECASE,
)


def _clean_decedent_name(name: str) -> str:
    """Strip trailing legal-qualifier phrases from a decedent name.

    Probate notices often append clarifying phrases (", A Protected Person,
    Now Deceased" / ", A.K.A. ..." / ", an Adult") that get captured by
    the DECEDENT_NAME_RE non-greedy match. These phrases break the
    downstream property locator (searches "Jean Sutliffe A Protected Person
    Now" instead of "Jean Sutliffe") so we strip them here before storing.
    """
    if not name:
        return name
    cleaned = _DECEDENT_NAME_SUFFIX_RE.sub("", name).strip()
    # Trim trailing punctuation
    cleaned = cleaned.rstrip(",.;:")
    return cleaned


def _is_valid_name(name: str) -> bool:
    """Reject names that are obviously not real person/entity names."""
    lower = name.strip().lower()
    if lower in _INVALID_NAMES:
        return False
    for bad in _INVALID_NAMES:
        if lower.startswith(bad):
            return False
    if len(name) > 80 or len(name) < 3:
        return False
    return True


# Structured metadata from the detail page (labeled fields)
PUBLISH_DATE_RE = re.compile(
    r"Notice Publish Date:\s*\n?\s*(?:\w+day,\s*)?(\w+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


# ── Auction / sale date patterns ─────────────────────────────────────
# These phrases appear in foreclosure notices before the scheduled sale date.
# The date may be "Month DD, YYYY", "MM/DD/YYYY", or with a day-of-week prefix.

# Reusable date fragment: matches "March 18, 2026", "MARCH 27, 2026",
# "04/17/2025", optional day-of-week prefix like "Wednesday, " or "FRIDAY, "
_DATE_FRAGMENT = (
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*)?"
    r"("
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}\s*,?\s*\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")"
)

# Probate-sale date patterns (defined here because they depend on _DATE_FRAGMENT)
PETITION_FILED_RE = re.compile(
    r"(?:Petition\b[\s\S]{0,80}?\bwas\s+filed|filed\s+(?:a\s+petition|on))\s+on\s+"
    + _DATE_FRAGMENT,
    re.IGNORECASE,
)
HEARING_DATE_RE = re.compile(
    r"(?:"
    r"hearing\s+(?:is\s+set\s+(?:for\s+)?on\s+|will\s+be\s+held\s+on\s+|on\s+)"
    r"|to\s+be\s+heard\s+on\s+"
    r")"
    + _DATE_FRAGMENT,
    re.IGNORECASE,
)

AUCTION_DATE_PATTERNS = [
    # "Sale at public auction will be on March 18, 2026"
    re.compile(
        r"(?:sale\s+at\s+public\s+auction|public\s+auction\s+sale)\s+will\s+be\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "will, on March 5, 2026" / "will on March 5, 2026"
    re.compile(
        r"will\s*,?\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "Sale Date and Location: MARCH 6, 2026" / "Sale Date: March 6, 2026"
    re.compile(
        r"Sale\s+Date\s*(?:and\s+\w+)?\s*:\s*" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "will be sold ... on March 5, 2026" (within 60 chars)
    re.compile(
        r"will\s+be\s+sold\b.{0,60}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # "sale will be on March 5, 2026" / "sale will be held on ..."
    re.compile(
        r"sale\s+will\s+be\s+(?:held\s+)?on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "sell at public auction ... on March 5, 2026" (within 80 chars)
    re.compile(
        r"sell\s+at\s+public\s+auction\b.{0,80}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # "proceed to sell ... on 3/5/2026" (within 80 chars)
    re.compile(
        r"proceed\s+to\s+sell\b.{0,80}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # "the 12th day of February, 2026" — ordinal date format
    re.compile(
        r"the\s+(\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s*,?\s*\d{4})",
        re.IGNORECASE,
    ),
    # "sell the property described on: Friday, February 20, 2026"
    re.compile(
        r"(?:sell|advertise)\s+the\s+property\s+described\s+on\s*:\s*" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # ", on DATE[,] at/on or about HH:MM" — sale scheduled with specific time
    re.compile(
        r",\s+on\s+" + _DATE_FRAGMENT + r"\s*,?\s+(?:at|on)\s+(?:or\s+about\s+)?\d{1,2}:\d{2}",
        re.IGNORECASE,
    ),
    # "notice is hereby given that on DATE" — HUD foreclosure notices
    re.compile(
        r"notice\s+is\s+hereby\s+given\s+that\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # AL convention: "will sell at public outcry [...] on DATE"
    # ".{0,250}" because AL notices interpose the trustee, courthouse,
    # county, and city between "will sell" and "on DATE".
    re.compile(
        r"will\s+sell\s+at\s+public\s+(?:outcry|auction)\b.{0,250}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # AL continuation: "It will be held on DATE" or "sale will be held on DATE"
    # Anchored on "it"/"sale" to avoid matching unrelated "will be held on" prose.
    re.compile(
        r"(?:\bit|\bsale)\s+will\s+be\s+held\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # AL trailing anchor: ", on DATE, during the legal hours of sale"
    # AL equivalent of the time-based pattern above — uses statutory phrase
    # "legal hours of sale" instead of a specific time like "at 11:00".
    re.compile(
        r",\s+on\s+" + _DATE_FRAGMENT + r"\s*,?\s+during\s+the\s+legal\s+hours",
        re.IGNORECASE,
    ),
]

# Postponement-chain pattern: captures the LATEST scheduled sale date when a
# notice has been continued/postponed. Two AL conventions covered:
#   1. "postponed from <OLD> until <NEW>"  — historic phrasing
#   2. "sale has been continued. [...] will be held on <NEW>"  — current AL
#      newspaper convention. The "originally set for <OLD>" sentence that
#      typically follows is intentionally NOT matched (we want NEW, not OLD).
# Alabama foreclosure notices append every continuation to the original notice
# text rather than re-publishing, so the LAST match in the document is the
# currently-scheduled sale date.
POSTPONEMENT_RE = re.compile(
    r"(?:"
    r"postponed\s+from\s+[\w\s,]+?\s+until"
    r"|"
    r"sale\s+has\s+been\s+continued[\s\S]{0,100}?will\s+be\s+held\s+on"
    r")\s+" + _DATE_FRAGMENT,
    re.IGNORECASE,
)


# ── County validation ────────────────────────────────────────────────
# These patterns detect when a notice's actual property is in a different
# county than the saved search that returned it (false positive from keyword match).

# Register/probate-court phrasings. Several variations seen across AL counties:
#   "Office of the Judge of Probate of Jefferson County"     (Jefferson foreclosure)
#   "Judge of Probate of Pike County"                        (Pike Notice-to-Creditors)
#   "Judge of the Probate Court of Lauderdale County"        (Lauderdale Letters of Admin)
#   "Probate Court of Houston County, Alabama"               (Houston summary distribution)
#   "Register's Office of/for X County"                      (TN holdover)
_REGISTER_COUNTY_RE = re.compile(
    r"(?:Office\s+of\s+the\s+Judge\s+of\s+Probate\s+of"
    r"|Judge\s+of\s+(?:the\s+)?Probate(?:\s+Court)?\s+of"
    r"|Probate\s+Court\s+of"
    r"|Register'?s\s+Office\s+(?:for|of))\s+(\w+)\s+County",
    re.IGNORECASE,
)

# "{County} County Courthouse" or "{County} County, Alabama" — note
# \s* (not \s+) so "County, Alabama" matches when no whitespace separates
# County and the comma (the standard AL probate phrasing).
_COURTHOUSE_COUNTY_RE = re.compile(
    r"(\w+)\s+County\s*(?:Courthouse|,\s*Alabama)",
    re.IGNORECASE,
)

# "Publication County: Jefferson" — Alabama Public Notices site header field
_PUBLICATION_COUNTY_RE = re.compile(
    r"Publication\s+County\s*:\s*(\w+)",
    re.IGNORECASE,
)

# AL pleading-header convention: "STATE OF ALABAMA / COUNTY OF LAUDERDALE"
# (or "COUNTY OF X" inline). Anchors the keyword "COUNTY OF" so we don't
# match arbitrary genitive uses ("...the county of jefferson...").
_COUNTY_OF_RE = re.compile(
    r"\bCOUNTY\s+OF\s+([A-Za-z][A-Za-z\-]+)\b",
    re.IGNORECASE,
)

# Reverse AL pleading-header: "STATE OF ALABAMA, / LIMESTONE COUNTY." or
# "STATE OF ALABAMA / X COUNTY" — comma/period/whitespace between STATE and
# the county-name token. Distinct from _COURTHOUSE_COUNTY_RE which requires
# "X County, Alabama" (state-name AFTER county); this pattern handles
# state-name BEFORE county. Anchored on STATE OF ALABAMA so it can't pick
# up arbitrary "X County" mentions.
_STATE_HEADER_COUNTY_RE = re.compile(
    r"STATE\s+OF\s+ALABAMA[,.\s]+([A-Za-z][A-Za-z\-]+)\s+COUNTY",
    re.IGNORECASE,
)

# Counties we care about — notices for other counties are false positives
_TARGET_COUNTIES = {"jefferson", "madison", "marshall"}


def _detect_counties_via_regex(text: str) -> set[str]:
    """Run all county-detection regexes and return the union as lowercase names."""
    register_matches = _REGISTER_COUNTY_RE.findall(text)
    courthouse_matches = _COURTHOUSE_COUNTY_RE.findall(text)
    publication_matches = _PUBLICATION_COUNTY_RE.findall(text)
    county_of_matches = _COUNTY_OF_RE.findall(text)
    state_header_matches = _STATE_HEADER_COUNTY_RE.findall(text)

    mentioned = set()
    for c in (register_matches + courthouse_matches + publication_matches
              + county_of_matches + state_header_matches):
        mentioned.add(c.lower())
    return mentioned


def snippet_passes_county_filter(snippet: str) -> bool:
    """Cheap pre-CAPTCHA county pre-filter for APN search-result snippets.

    APN's search box has no county filter (statewide full-text) and probate
    searches like "Estate Deceased" return ~1,000 notices per 14-day window
    across all 67 Alabama counties. Without a pre-filter, every notice gets
    CAPTCHA-solved at ~$0.003 each just to apply the downstream county check.

    This function reads the 2KB result-row snippet (free — no CAPTCHA) and
    drops notices that clearly belong to a non-target county. Decision rules:

    - Snippet mentions ≥1 target county (jefferson/madison/marshall): KEEP.
      Will pass the full post-CAPTCHA county filter.
    - Snippet mentions a non-target county AND no target county: DROP. The
      notice is for some other Alabama county and we don't want it.
    - Snippet mentions no county at all: KEEP (don't risk a false negative —
      some notices have the county only in the body, not the snippet header).

    Trades a small false-negative rate (notices whose snippet is too short
    to contain the county marker) for a ~80%+ CAPTCHA-cost reduction on
    statewide searches like probate Notice-to-Creditors.
    """
    if not snippet:
        return True
    mentioned = _detect_counties_via_regex(snippet)
    if not mentioned:
        # No county detected anywhere — can't tell, keep it (cheap to
        # CAPTCHA + drop downstream than to miss a real target hit).
        return True
    if mentioned & _TARGET_COUNTIES:
        return True
    # Snippet mentions only non-target counties — safe to drop.
    return False


def is_target_county(text: str, search_county: str) -> bool:
    """Synchronous regex-only county check (no LLM tiebreaker).

    Use is_target_county_async() when an api_key is available to enable
    LLM-based fallback for ambiguous cases (zero matches or multiple
    counties detected).

    Returns True if the property appears to be in Knox/Blount/Jefferson/
    Madison county, or if we can't determine the county at all (benefit
    of the doubt).
    """
    mentioned_counties = _detect_counties_via_regex(text)
    if not mentioned_counties:
        return True  # Can't determine — keep it

    # If ANY of our target counties appear, keep the notice
    if mentioned_counties & _TARGET_COUNTIES:
        return True

    # Only non-target counties found — this is a false positive
    logger.info(
        "County mismatch: search='%s' but property in %s — filtering out",
        search_county, ", ".join(sorted(mentioned_counties)).title(),
    )
    return False


async def is_target_county_async(
    text: str, search_county: str, api_key: str | None = None,
) -> bool:
    """County check with LLM tiebreaker for ambiguous regex results.

    Decision tree:
      0. Notice text is too short (failed CAPTCHA / failed PDF extract /
         placeholder page) → REJECT immediately. Empty text would otherwise
         trigger the benefit-of-doubt path and waste downstream pipeline
         work on records that have no parseable content.
      1. Regex finds exactly ONE county that's in target → KEEP (no LLM)
      2. Regex finds exactly ONE county not in target → REJECT (no LLM)
      3. Regex finds ZERO counties → ask LLM (if api_key available)
      4. Regex finds MULTIPLE counties → ask LLM to disambiguate
      5. No api_key OR LLM returns nothing → fall back to regex behavior
         (benefit-of-doubt for empty, intersect-test for multiple)

    Most common-case clean notices stay regex-only. LLM only fires for
    notices with ambiguous county signals — typically <10% of probate
    notices, even less for foreclosure.
    """
    # Step 0: bail out if there's no real content to evaluate. AL probate
    # notices that survive the captcha but fail the embedded-PDF text
    # extraction return < 200 chars of placeholder text ("Back / Please
    # view the PDF for the complete Public Notice."). Without this guard,
    # the benefit-of-doubt rule keeps them as Jefferson — they then waste
    # downstream LLM/Tracerfy/validation cost only to be rejected.
    if not text or len(text.strip()) < 200:
        logger.debug(
            "Notice text too short (%d chars) — rejecting (likely failed "
            "CAPTCHA/PDF extraction)",
            len(text.strip()) if text else 0,
        )
        return False

    mentioned_counties = _detect_counties_via_regex(text)

    # Confident single-match cases — no LLM needed
    if len(mentioned_counties) == 1:
        return mentioned_counties.issubset(_TARGET_COUNTIES) or bool(
            mentioned_counties & _TARGET_COUNTIES
        )

    # Ambiguous: zero or multiple county signals → LLM tiebreaker
    if api_key:
        try:
            from llm_parser import extract_county_from_notice
            llm_county = await extract_county_from_notice(text, api_key, rate_tracker=rate_tracker)
        except Exception as e:
            logger.debug("LLM county lookup error: %s", e)
            llm_county = ""
        if llm_county:
            keep = llm_county in _TARGET_COUNTIES
            if not keep:
                logger.info(
                    "County mismatch (LLM): regex=%s LLM=%s — filtering out",
                    sorted(mentioned_counties) or "[]", llm_county.title(),
                )
            else:
                logger.debug("County resolved via LLM: %s", llm_county)
            return keep

    # No LLM or LLM said unknown — fall back to regex-only behavior
    if not mentioned_counties:
        return True  # benefit of doubt
    if mentioned_counties & _TARGET_COUNTIES:
        return True
    logger.info(
        "County mismatch: search='%s' but property in %s — filtering out",
        search_county, ", ".join(sorted(mentioned_counties)).title(),
    )
    return False


# ── Main parser ───────────────────────────────────────────────────────


async def _try_extract_pdf_text(page: Page) -> str:
    """Try to extract full text from the PDF embedded on the notice detail page.

    The site embeds a PDF viewer above the web text.  When the web display is
    truncated to 1,000 characters, the full text may only be available in the PDF.
    We look for an <iframe> or <embed>/<object> with a PDF src and download it.
    """
    try:
        # Look for PDF iframe, embed, or object element.
        # Alabama site serves PDFs via ASP.NET handlers (no .pdf extension in URL).
        pdf_url = await page.evaluate("""() => {
            // iframe — .pdf extension OR ASP.NET document handlers
            for (const f of document.querySelectorAll('iframe')) {
                if (!f.src) continue;
                if (f.src.includes('.pdf') || f.src.includes('PDF') ||
                    f.src.includes('GetDocument') || f.src.includes('getdocument') ||
                    f.src.includes('ViewDocument') || f.src.includes('Document.aspx') ||
                    f.src.includes('NoticeDocument') || f.src.includes('getpdf') ||
                    f.src.includes('PublicNotice')) return f.src;
            }
            // embed element
            const embed = document.querySelector('embed[src*=".pdf"], embed[type="application/pdf"]');
            if (embed) return embed.src;
            // object element
            const obj = document.querySelector('object[data*=".pdf"], object[type="application/pdf"]');
            if (obj) return obj.data;
            // Link to PDF
            const link = document.querySelector('a[href*=".pdf"]');
            if (link) return link.href;
            // Any iframe on the page (last resort — the document viewer)
            const anyIframe = document.querySelector('iframe[src]');
            if (anyIframe && anyIframe.src && !anyIframe.src.includes('recaptcha') &&
                !anyIframe.src.includes('google') && !anyIframe.src.includes('captcha'))
                return anyIframe.src;
            return null;
        }""")

        if not pdf_url:
            return ""

        logger.info("Found PDF URL: %s", pdf_url[:120])

        # Download the PDF using the page's browser context (inherits cookies/session)
        response = await page.context.request.get(pdf_url)
        if response.status != 200:
            logger.warning("PDF download failed: HTTP %d", response.status)
            return ""

        pdf_bytes = await response.body()

        from io import BytesIO

        # Try pdfminer first — fast text-layer extraction (Jefferson County text PDFs)
        try:
            from pdfminer.high_level import extract_text as pdfminer_extract
            text = pdfminer_extract(BytesIO(pdf_bytes))
            if text and len(text.strip()) > 100:
                logger.info("PDF text extracted via pdfminer: %d chars", len(text))
                return text.strip()
        except ImportError:
            logger.debug("pdfminer not available")
        except Exception as e:
            logger.warning("pdfminer extraction failed: %s", e)

        # OCR fallback — for image-based PDFs (Madison County newspaper scans)
        # pypdfium2 renders each page to an image, then Tesseract reads the text.
        logger.info("No text layer found — attempting OCR on PDF pages")
        try:
            import pypdfium2 as pdfium
            from image_utils import ocr_page

            doc = pdfium.PdfDocument(BytesIO(pdf_bytes))
            ocr_pages = []
            for i in range(len(doc)):
                pdf_pg = doc[i]
                bitmap = pdf_pg.render(scale=200 / 72)  # 200 DPI — good balance of speed vs accuracy
                pil_img = bitmap.to_pil()
                page_text = ocr_page(pil_img, psm=3)   # PSM 3 = fully automatic (newspaper layout)
                if page_text.strip():
                    ocr_pages.append(page_text)
            doc.close()
            combined = "\n\n".join(ocr_pages)
            if len(combined.strip()) > 50:
                logger.info("PDF OCR extracted %d chars from %d pages", len(combined), len(ocr_pages))
                return combined.strip()
        except ImportError:
            logger.debug("pypdfium2 not available — cannot OCR image PDF")
        except Exception as e:
            logger.warning("PDF OCR failed: %s", e)

    except Exception as e:
        logger.debug("PDF URL detection failed: %s", e)

    return ""


async def parse_notice_page(
    page: Page, county: str, notice_type: str, llm_api_key: str | None = None,
    *, rate_tracker: "ServiceRateTracker | None" = None,
) -> NoticeData:
    """Extract structured fields from a notice detail page (after CAPTCHA solve).

    Uses both the structured metadata labels on the page and regex parsing
    of the notice body text.  When llm_api_key is provided, falls back to
    Claude Haiku for any fields the regex parser couldn't extract.
    """
    notice = NoticeData(
        county=county,
        notice_type=notice_type,
        source_url=page.url,
        received_date=datetime.now().strftime("%Y-%m-%d"),
    )

    # Get the full page text (includes both metadata labels and notice body)
    full_text = await page.inner_text("body")

    # Normalize non-breaking spaces → regular spaces (breaks \s+ regex matching)
    full_text = full_text.replace("\xa0", " ")

    # Extract the notice body content (after "Notice Content" header)
    notice_content = _extract_notice_content(full_text)

    # Try PDF when content is truncated (Jefferson County) OR absent/very short
    # (Madison County — detail page is a newspaper image-PDF with no web text)
    _content_short = not notice_content or len((notice_content or "").strip()) < 200
    _content_truncated = bool(notice_content and "Web display limited to" in notice_content)
    if _content_truncated or _content_short:
        pdf_text = await _try_extract_pdf_text(page)
        if pdf_text:
            notice_content = pdf_text

    # Normalize PDF line-break hyphens and smart quotes so regex parsers
    # work on flowing text rather than column-wrapped fragments. Critical for
    # Madison County notices (newspaper PDFs); harmless for Jefferson (already-flat text).
    notice.raw_text = _normalize_pdf_text(notice_content if notice_content else full_text)

    if not notice.raw_text.strip():
        logger.warning("No notice text found on %s", page.url)
        return notice

    # ── Extract structured metadata from labels ────────────────────
    notice.date_added = _extract_publish_date(full_text)

    # ── Extract fields from the notice body text ───────────────────
    _parse_address(notice)
    _parse_name(notice)
    _split_owner_name(notice)
    _split_decedent_name(notice)
    _parse_pr_address(notice)
    _parse_probate_metadata(notice)
    if notice_type != "probate":
        _parse_auction_date(notice)
    _parse_foreclosure_parties(notice)

    # ── LLM fallback for missing fields ──────────────────────────
    # pre_probate (APN "Notice of Death" publications) is shaped like probate
    # but typically lacks case_number / granted_date — we just need
    # decedent_name + optional contact. Reuse probate trigger conditions
    # without the strict case/judge/granted requirements.
    needs_llm = (
        (notice_type == "probate" and (
            not notice.owner_name or not notice.decedent_name or not notice.owner_street
            or not notice.case_number or not notice.judge_name or not notice.granted_date
        ))
        or (notice_type == "pre_probate" and (
            not notice.decedent_name or not notice.owner_name
        ))
        or (notice_type == "foreclosure" and (
            not notice.address or not notice.owner_name or not notice.auction_date
            or not notice.mortgage_company or not notice.trustee
        ))
        or (notice_type not in ("probate", "pre_probate", "foreclosure") and (
            not notice.address or not notice.owner_name or not notice.auction_date
        ))
    )
    if llm_api_key and needs_llm:
        from llm_parser import extract_with_llm

        llm_result = await extract_with_llm(
            notice.raw_text, notice_type, county, llm_api_key,
            rate_tracker=rate_tracker,
        )

        if notice_type == "probate":
            # Probate: fill decedent name, PR name, and PR mailing address
            if not notice.decedent_name and llm_result.get("decedent_name"):
                cand = _clean_decedent_name(llm_result["decedent_name"].strip())
                if _is_valid_name(cand):
                    notice.decedent_name = cand
                    logger.info("LLM filled decedent: %s", notice.decedent_name)
            if not notice.owner_name and llm_result.get("owner_name"):
                cand = llm_result["owner_name"].strip()
                if _is_valid_name(cand):
                    notice.owner_name = cand
                    logger.info("LLM filled PR: %s", notice.owner_name)
                else:
                    logger.debug("LLM PR rejected as junk: %r", cand)
            if not notice.owner_street and llm_result.get("owner_street"):
                notice.owner_street = llm_result["owner_street"]
                notice.owner_city = llm_result.get("owner_city") or notice.owner_city
                # Source-text-anchored validation. LLMs hallucinate states
                # (2026-06-20: 8/20 AL probates returned owner_state="TN"
                # despite no "TN"/"Tennessee" in the source). Trust the
                # LLM only when the extracted state literally appears in
                # the notice text — that's how real out-of-state mailing
                # addresses (California heir, Knoxville TN executor on an
                # AL decedent) get through cleanly while hallucinations
                # fall back to the property state.
                from state_resolver import state_for_county, validate_person_state
                property_state = state_for_county(notice.county)
                notice.owner_state = validate_person_state(
                    llm_result.get("owner_state"),
                    notice.raw_text,
                    fallback_state=property_state,
                )
                notice.owner_zip = llm_result.get("owner_zip") or notice.owner_zip
                logger.info("LLM filled PR address: %s", notice.owner_street)
            # Probate metadata (case#, judge, granted date)
            if not notice.case_number and llm_result.get("case_number"):
                notice.case_number = re.sub(r"\s+", "", llm_result["case_number"]).upper()
            if not notice.judge_name and llm_result.get("judge_name"):
                notice.judge_name = llm_result["judge_name"]
            if not notice.granted_date and llm_result.get("granted_date"):
                notice.granted_date = llm_result["granted_date"]
            # Re-run probate metadata so creditor_deadline gets computed if granted_date arrived
            _parse_probate_metadata(notice)
        else:
            # Foreclosure / tax sale / tax lien
            if not notice.address and llm_result.get("address"):
                notice.address = llm_result["address"]
                notice.city = llm_result.get("city") or notice.city
                notice.zip = llm_result.get("zip") or notice.zip
                logger.info("LLM filled address: %s", notice.address)
            if not notice.owner_name and llm_result.get("owner_name"):
                cand = llm_result["owner_name"].strip()
                if _is_valid_name(cand):
                    notice.owner_name = cand
                    logger.info("LLM filled owner: %s", notice.owner_name)
                else:
                    logger.debug("LLM owner rejected as junk: %r", cand)
            if not notice.auction_date and llm_result.get("auction_date"):
                notice.auction_date = llm_result["auction_date"]
                logger.info("LLM filled auction_date: %s", notice.auction_date)
            # Foreclosure-only party fields
            if notice_type == "foreclosure":
                if not notice.mortgage_company and llm_result.get("mortgage_company"):
                    notice.mortgage_company = llm_result["mortgage_company"]
                if not notice.original_lender and llm_result.get("original_lender"):
                    notice.original_lender = llm_result["original_lender"]
                if not notice.trustee and llm_result.get("trustee"):
                    notice.trustee = llm_result["trustee"]
                if not notice.trustee_file_number and llm_result.get("trustee_file_number"):
                    notice.trustee_file_number = llm_result["trustee_file_number"]
            # Eviction-specific fields. Plaintiff already filled via owner_name
            # above; here we capture case # and back-rent owed. amount_owed
            # rides in tax_delinquent_amount (DataSift schema has no separate
            # eviction-amount column) so the existing tax_high_exposure tag
            # fires when ≥ $5K — high back-rent = motivated landlord.
            if notice_type == "eviction":
                if not notice.case_number and llm_result.get("case_number"):
                    notice.case_number = re.sub(r"\s+", "", llm_result["case_number"]).upper()
                if not notice.tax_delinquent_amount and llm_result.get("amount_owed"):
                    notice.tax_delinquent_amount = llm_result["amount_owed"]

    # Re-run name splits in case the LLM filled owner_name / decedent_name
    _split_owner_name(notice)
    _split_decedent_name(notice)

    return notice


# ── Metadata extractors ──────────────────────────────────────────────


def _normalize_pdf_text(text: str) -> str:
    """De-hyphenate line-wrapped words and normalize smart quotes.

    Newspaper PDFs (Madison County, AL) wrap text at narrow column widths and
    insert soft hyphens at line breaks: "in-\nformational", "Hunts-\nville",
    "post-\nponed". Joining these is required for any regex that spans more
    than one word. Also normalizes smart quotes (used in trustee designations
    like ("Transferee")) to ASCII so single regexes match both formats.
    """
    if not text:
        return text
    # "word-\nword" → "wordword" (keeps real hyphenated names like "Smith-Jones")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Smart/curly quotes → straight quotes
    text = (text
            .replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"'))
    # Non-breaking spaces → regular spaces
    text = text.replace("\xa0", " ")
    return text


def _extract_notice_content(full_text: str) -> str:
    """Pull just the notice body from the full page text.

    The page has a "Notice Content" label followed by the actual legal text,
    then "Back" and footer content.
    """
    # Find "Notice Content" section
    marker = "Notice Content"
    idx = full_text.find(marker)
    if idx == -1:
        return ""

    body = full_text[idx + len(marker):]

    # Trim at the footer / "Back" link / language selector
    for end_marker in ["\nBack\n", "\nIf you have any questions", "\nSelect Language"]:
        end_idx = body.find(end_marker)
        if end_idx != -1:
            body = body[:end_idx]
            break

    return body.strip()


def _extract_publish_date(full_text: str) -> str:
    """Extract the Notice Publish Date from the structured metadata labels."""
    m = PUBLISH_DATE_RE.search(full_text)
    if m:
        return _normalize_date(m.group(1))
    return ""


def _normalize_date(raw: str) -> str:
    """Convert various date formats to YYYY-MM-DD."""
    raw = raw.strip().rstrip(".")

    # Handle ordinal format: "12th day of February, 2026" → "February 12, 2026"
    ordinal_m = re.match(
        r"(\d{1,2})(?:st|nd|rd|th)\s+day\s+of\s+(\w+)\s*,?\s*(\d{4})",
        raw, re.IGNORECASE,
    )
    if ordinal_m:
        raw = f"{ordinal_m.group(2)} {ordinal_m.group(1)}, {ordinal_m.group(3)}"

    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _parse_auction_date(notice: NoticeData) -> None:
    """Extract the scheduled sale/auction date from the notice body text.

    For Alabama foreclosure notices, sales are often postponed multiple times
    in the same notice text (e.g. "postponed from December 22, 2025 until
    January 29, 2026 ... postponed from January 29, 2026 until June 4, 2026").
    The LATEST postponement is the actual sale date, so we look for that first
    and only fall back to the original sale-date patterns if no postponement
    chain is present.
    """
    text = notice.raw_text

    # Find ALL postponement targets and pick the last one (chronological order
    # in the document = chronological order of postponements).
    postpone_matches = POSTPONEMENT_RE.findall(text)
    if postpone_matches:
        latest = postpone_matches[-1].strip()
        normalized = _normalize_date(latest)
        if normalized and len(normalized) >= 8:
            notice.auction_date = normalized
            return

    # No postponement chain — use the first matching original-sale-date pattern.
    for pattern in AUCTION_DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            raw_date = m.group(1).strip()
            normalized = _normalize_date(raw_date)
            if normalized and len(normalized) >= 8:
                notice.auction_date = normalized
                return


# ── Address extraction ───────────────────────────────────────────────


def _parse_address(notice: NoticeData) -> None:
    """Extract property address, city, and zip from the notice body text.

    Strategy (in priority order):
      1. Full contextual match: "commonly known as ADDRESS, CITY, TN ZIP"
         → extracts address + city + zip from the same phrase
      2. Address-only contextual: "commonly known as ADDRESS"
         → extracts address, then finds city/zip nearby
      3. "located at" pattern: "located at ADDRESS, CITY, TN ZIP"
         → secondary, used for tax sales (validated against blacklist)
      4. Give up — leave fields empty (better than grabbing wrong address)
    """
    text = notice.raw_text.replace("\xa0", " ")

    # ── Strategy 1: Full context — indicator + address + city + TN + zip ──
    m = FULL_PROPERTY_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        if _is_valid_address(addr):
            notice.address = addr
            notice.city = _clean_city(m.group(2))
            if m.group(3):
                notice.zip = m.group(3)
            return

    # ── Strategy 2: Address-only context — indicator + address ──
    m = PROPERTY_ADDR_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        if _is_valid_address(addr):
            notice.address = addr
            # Try to find city and zip near the matched address
            _extract_city_zip_near(notice, text, m.end())
            return

    # ── Strategy 3: "located at" (for tax sales, etc.) ──
    m = LOCATED_AT_FULL_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        # Extra validation: reject if near "sale" or "auction" context
        context_before = _get_context_before(text, m.start(), 80)
        is_sale_location = any(w in context_before for w in [
            "sale", "auction", "held", "entrance", "courthouse",
        ])
        if not is_sale_location and _is_valid_address(addr):
            notice.address = addr
            notice.city = _clean_city(m.group(2))
            if m.group(3):
                notice.zip = m.group(3)
            return

    m = LOCATED_AT_ADDR_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        context_before = _get_context_before(text, m.start(), 80)
        is_sale_location = any(w in context_before for w in [
            "sale", "auction", "held", "entrance", "courthouse",
        ])
        if not is_sale_location and _is_valid_address(addr):
            notice.address = addr
            _extract_city_zip_near(notice, text, m.end())
            return

    # ── Strategy 4: Standalone "ADDRESS, CITY, TN ZIP" for tax types ──
    # Tax sale / tax lien notices sometimes list the address without an
    # indicator phrase. We only try this for those types and validate
    # against known bad addresses and auction context.
    if notice.notice_type in ("tax_sale", "tax_lien"):
        m = STANDALONE_ADDR_RE.search(text)
        if m:
            addr = _clean_address(m.group(1))
            city = _clean_city(m.group(2))
            # Reject if near sale/auction context
            context_before = _get_context_before(text, m.start(), 100)
            is_sale_ctx = any(w in context_before for w in [
                "sale", "auction", "held at", "entrance", "courthouse",
                "conducted at", "front door",
            ])
            if not is_sale_ctx and _is_valid_address(addr) and city in _KNOWN_CITIES_SET:
                notice.address = addr
                notice.city = city
                if m.group(3):
                    notice.zip = m.group(3)
                return

    # ── Strategy 5: No confident match → leave address empty ──
    # Still try to extract city/zip if they appear in context
    _extract_city_zip_fallback(notice, text)


def _get_context_before(text: str, pos: int, chars: int) -> str:
    """Get lowercase text in the window before a position."""
    s: int = pos - chars
    if s < 0:
        s = 0
    return text[s:pos].lower()


def _extract_city_zip_near(notice: NoticeData, text: str, addr_end: int) -> None:
    """Extract city and zip from the text near the end of the address match.

    Looks in the 200 characters after the address for "City, TN ZIP" or
    "City, Tennessee ZIP".
    """
    window = text[addr_end:addr_end + 200]

    # Try "CITY, [County,] TN ZIP" or "CITY, [County,] Tennessee ZIP"
    city_state_re = re.compile(
        r"[,.\s]+([\w][\w\s]*?)"
        r"(?:\s*[,.]\s*\w+\s+County)?"   # optional county
        r"\s*[,.]\s*(?:Tennessee|Tenn\.?|TN)"
        r"\s*[,.\s]*(\d{5}(?:-\d{4})?)?",
        re.IGNORECASE,
    )
    m = city_state_re.search(window)
    if m:
        notice.city = _clean_city(m.group(1))
        if m.group(2):
            notice.zip = m.group(2)
        return

    # Fallback: find a known TN city in the window
    window_upper = window.upper()
    for city in TN_CITIES:
        if city.upper() in window_upper:
            notice.city = city
            break

    # Find a TN zip near the address
    zip_match = ZIP_RE.search(window)
    if zip_match:
        notice.zip = zip_match.group(1)


def _is_valid_fallback_zip(zip_code: str, county: str) -> bool:
    """Check if a zip found via fallback (no address context) is plausible."""
    if zip_code in _COURTHOUSE_ZIPS:
        return False
    prefixes = _COUNTY_ZIP_PREFIXES.get(county)
    if prefixes and not any(zip_code.startswith(p) for p in prefixes):
        return False
    return True


def _extract_city_zip_fallback(notice: NoticeData, text: str) -> None:
    """Last resort: find city/zip anywhere in the notice text.

    Only used when no address was found. Finds the first known TN city
    and first TN zip code, but rejects courthouse/out-of-county zips.
    """
    if not notice.city:
        text_upper = text.upper()
        for city in TN_CITIES:
            if city.upper() in text_upper:
                notice.city = city
                break

    if not notice.zip:
        for zip_match in ZIP_RE.finditer(text):
            candidate = zip_match.group(1)
            if not _is_valid_fallback_zip(candidate, notice.county):
                continue
            notice.zip = candidate
            break


def _clean_address(raw: str) -> str:
    """Normalize whitespace and trailing punctuation in an extracted address."""
    addr = re.sub(r"\s+", " ", raw).strip()
    addr = addr.rstrip(",. ")
    return addr


def _clean_city(raw: str) -> str:
    """Clean up an extracted city name."""
    city = re.sub(r"\s+", " ", raw).strip()
    city = city.rstrip(",. ")
    # Title-case if all uppercase
    if city.isupper():
        city = city.title()
    return city


# ── Name extraction ──────────────────────────────────────────────────


def _parse_name(notice: NoticeData) -> None:
    """Extract owner/party name based on notice type."""
    text = notice.raw_text.replace("\xa0", " ")

    if notice.notice_type == "probate":
        # Extract decedent name from "Estate of [NAME], Deceased"
        dec_match = DECEDENT_NAME_RE.search(text)
        if dec_match:
            dec_name = _clean_name(dec_match.group(1))
            # Strip legal-qualifier phrases like ", A Protected Person, Now",
            # ", A.K.A. ...", ", an Adult" — they break the property locator.
            dec_name = _clean_decedent_name(dec_name)
            if _is_valid_name(dec_name):
                notice.decedent_name = dec_name

        # Extract PR/Executor name. Try patterns in priority order, validating each:
        #   (a) AL "granted to NAME on/as..." — most reliable when present
        #   (b) AL "NAME\nPersonal Representative" — signature-block format
        #   (c) TN "Personal Representative: NAME" — original/legacy
        # Skip a match if the captured token is in the invalid-names list (e.g.
        # "the undersigned" appears in some AL notices in place of the PR name).
        for pat in (PROBATE_NAME_GRANTED_RE, PROBATE_NAME_BEFORE_TITLE_RE, PROBATE_NAME_RE):
            m = pat.search(text)
            if not m:
                continue
            candidate = _clean_name(m.group(1))
            if _is_valid_name(candidate):
                notice.owner_name = candidate
                break
        return

    # Foreclosure / tax sale / tax lien — "executed by" is the most common
    match = EXECUTED_BY_RE.search(text)
    if match:
        name = _clean_name(match.group(1))
        if _is_valid_name(name):
            notice.owner_name = name
            return

    # Fallback patterns
    for pattern in OWNER_PATTERNS:
        match = pattern.search(text)
        if match:
            name = _clean_name(match.group(1))
            if _is_valid_name(name):
                notice.owner_name = name
                return


# Suffix tokens to strip when splitting first/last names
_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _split_full_name(name: str) -> tuple[str, str, str, str]:
    """Split a full name into (first, middle, last, suffix).

    Handles "Alisha N. Vallery", "Curtis C Rush Sr",
    "Mary Angela Caylor Roling", "John Doe and Jane Doe" (uses first listed),
    "James F. Smith Jr.", and OCR'd variants. Empty strings for missing parts.
    """
    if not name:
        return ("", "", "", "")

    # If multiple people are joined by "and" / "&", use only the first
    primary = re.split(r"\s+(?:and|&)\s+", name, maxsplit=1)[0]
    # Drop trailing parenthetical / role descriptors
    primary = re.sub(r"\s*\([^)]*\)\s*$", "", primary).strip(" ,;.")

    tokens = primary.split()
    if not tokens:
        return ("", "", "", "")

    # Pop generational suffixes off the end (Jr, Sr, II, III, IV — with or without periods)
    suffixes_collected: list[str] = []
    suffix_canonical = {s.rstrip(".") for s in _NAME_SUFFIXES}
    while tokens and tokens[-1].lower().rstrip(".") in suffix_canonical:
        suffixes_collected.insert(0, tokens.pop())

    suffix_str = " ".join(suffixes_collected)
    if not tokens:
        return ("", "", "", suffix_str)

    first = tokens[0]
    if len(tokens) == 1:
        return (first, "", "", suffix_str)
    last = tokens[-1]
    middle = " ".join(tokens[1:-1])  # everything between first and last
    return (first, middle, last, suffix_str)


def _split_owner_name(notice: NoticeData) -> None:
    """Populate owner_first_name / owner_middle_name / owner_last_name / owner_suffix."""
    if not notice.owner_name:
        return
    if notice.owner_first_name or notice.owner_last_name:
        return  # already split
    first, middle, last, suffix = _split_full_name(notice.owner_name)
    notice.owner_first_name = first
    notice.owner_middle_name = middle
    notice.owner_last_name = last
    notice.owner_suffix = suffix


def _split_decedent_name(notice: NoticeData) -> None:
    """Populate decedent_first_name / middle / last / suffix from decedent_name."""
    if not notice.decedent_name:
        return
    if notice.decedent_first_name or notice.decedent_last_name:
        return
    first, middle, last, suffix = _split_full_name(notice.decedent_name)
    notice.decedent_first_name = first
    notice.decedent_middle_name = middle
    notice.decedent_last_name = last
    notice.decedent_suffix = suffix


# ── Foreclosure party regexes (mortgage company, trustee, file number) ─

# "the undersigned X, as Mortgagee/Transferee" — current servicer/holder.
# Character class allows \s so multi-line entity names ("Nationstar Mortgage\nLLC") match.
_MORTGAGEE_RE = re.compile(
    r"(?:the\s+)?undersigned\s+([A-Z][\w\s&,.\-]+?)\s*,?\s*"
    r"as\s+(?:Mortgagee|Transferee|Mortgagee\s*/\s*Transferee)",
    re.IGNORECASE,
)

# "originally in favor of X" — original lender (often MERS)
_ORIGINAL_LENDER_RE = re.compile(
    r"originally\s+in\s+favor\s+of\s+([A-Z][\w\s&,.\-]+?(?:Inc\.?|LLC|N\.?\s*A\.?|Corporation|Corp\.?|Bank|Company|Co\.?|Association|Mortgage|Trust|F\.?\s*S\.?\s*B\.?))"
    r"(?:\s+(?:its|as))",
    re.IGNORECASE,
)

# Trustee firm — law firm signature near the end of the notice.
# Pattern: '<Servicer Name>, ("Transferee") <Law Firm>, <street address>'
_TRUSTEE_RE = re.compile(
    r"\(\s*[\"']?Transferee[\"']?\s*\)\s*"
    r"([A-Z][\w\s&.\-]+?,\s*(?:P\.?\s*A\.?|P\.?\s*C\.?|LLC|L\.?L\.?P\.?|PLLC|LLP)\.?)",
    re.IGNORECASE,
)

# Trustee internal file number (e.g. "TB File Number: 25-40447-WF-AL")
_TRUSTEE_FILE_RE = re.compile(
    r"(?:File\s+(?:Number|No\.?|#)|Reference\s+No\.?|Our\s+File)\s*[:#]?\s*"
    r"([A-Z0-9][\w./\-]{4,30})",
    re.IGNORECASE,
)


def _parse_foreclosure_parties(notice: NoticeData) -> None:
    """Extract mortgage company, original lender, trustee, and file number.

    Foreclosure-only — these fields don't apply to probate / tax / eviction.
    """
    if notice.notice_type != "foreclosure":
        return

    text = notice.raw_text

    if not notice.mortgage_company:
        m = _MORTGAGEE_RE.search(text)
        if m:
            notice.mortgage_company = _clean_entity_name(m.group(1))

    if not notice.original_lender:
        m = _ORIGINAL_LENDER_RE.search(text)
        if m:
            notice.original_lender = _clean_entity_name(m.group(1))

    if not notice.trustee:
        m = _TRUSTEE_RE.search(text)
        if m:
            notice.trustee = _clean_entity_name(m.group(1))

    if not notice.trustee_file_number:
        m = _TRUSTEE_FILE_RE.search(text)
        if m:
            notice.trustee_file_number = m.group(1).strip()


def _parse_probate_metadata(notice: NoticeData) -> None:
    """Extract case#, judge, granted_date, creditor_deadline for AL probate notices.

    Alabama probate Notice-to-Creditors publications follow a predictable shape:
      - "Case No. PC<year>-<num>"  (e.g. PC2025-234, PR-2026-000557)
      - "having been granted to ... on the <day> day of <Month>, <year>"
      - "by the Honorable <Name>, Judge of Probate"
      - Recording stamp at top: "MM/DD/YYYY HH:MMam"

    Per Alabama Code § 43-2-350, creditors have 6 months from the granted date
    to file claims. We compute creditor_deadline = granted_date + 6 months.
    """
    if notice.notice_type != "probate":
        return

    text = notice.raw_text

    # Case number — top-of-document field; survives OCR cleanly
    if not notice.case_number:
        m = CASE_NUMBER_RE.search(text)
        if m:
            # Normalize: collapse internal whitespace, uppercase
            notice.case_number = re.sub(r"\s+", "", m.group(1)).upper()

    # Judge of Probate
    if not notice.judge_name:
        m = JUDGE_RE.search(text)
        if m:
            judge = _clean_name(m.group(1))
            if _is_valid_name(judge):
                notice.judge_name = judge

    # Granted date — try the prose pattern first; fall back to recording stamp
    if not notice.granted_date:
        m = GRANTED_DATE_RE.search(text)
        if m:
            # Branch 1: "the <day> day of <Month>, <year>" → groups 1, 2, 3
            # Branch 2: "<Month> <day>, <year>"           → groups 4, 5, 6
            if m.group(1):
                day, month, year = m.group(1), m.group(2), m.group(3)
            else:
                month, day, year = m.group(4), m.group(5), m.group(6)
            normalized = _normalize_date(f"{month} {day}, {year}")
            if normalized:
                notice.granted_date = normalized

        if not notice.granted_date:
            # Fallback: top-of-document recording stamp (always digit-clean)
            m = RECORDING_DATE_RE.search(text)
            if m:
                notice.granted_date = _normalize_date(m.group(1)) or ""

    # Creditor deadline = granted_date + 6 months (AL § 43-2-350)
    if notice.granted_date and not notice.creditor_deadline:
        try:
            from datetime import datetime
            dt = datetime.strptime(notice.granted_date, "%Y-%m-%d")
            # 6-month bump preserving day-of-month where possible
            month = dt.month + 6
            year = dt.year + (month - 1) // 12
            month = ((month - 1) % 12) + 1
            try:
                deadline = dt.replace(year=year, month=month)
            except ValueError:
                # day-of-month doesn't exist in target month (e.g. Aug 31 → Feb 31) — clamp to last
                deadline = dt.replace(year=year, month=month, day=28)
            notice.creditor_deadline = deadline.strftime("%Y-%m-%d")
        except (ValueError, ImportError):
            pass

    # Subtype detection + sale/heirs-specific extraction
    _parse_probate_subtype(notice)


def _parse_probate_subtype(notice: NoticeData) -> None:
    """Detect probate notice subtype and extract sale/heirs-specific fields.

    Three mutually-exclusive subtypes (in priority order):
      probate_sale         — PR petitioning court to approve real estate sale
      probate_heirs_notice — Notice TO named heirs about an estate proceeding
      probate_creditors    — default; standard 6-month creditor publication
    """
    text = notice.raw_text
    if not text:
        notice.notice_subtype = "probate_creditors"
        return

    # Subtype: probate_sale (highest signal — explicit petition language)
    if PROBATE_SALE_SIGNATURE_RE.search(text):
        notice.notice_subtype = "probate_sale"

        # Petition filing date
        m = PETITION_FILED_RE.search(text)
        if m and not notice.petition_filed_date:
            notice.petition_filed_date = _normalize_date(m.group(1).strip()) or ""

        # Hearing date
        m = HEARING_DATE_RE.search(text)
        if m and not notice.hearing_date:
            notice.hearing_date = _normalize_date(m.group(1).strip()) or ""

        # Estate purpose
        m = ESTATE_PURPOSE_RE.search(text)
        if m and not notice.estate_purpose:
            notice.estate_purpose = m.group(1).strip().rstrip(".,;:")

        # Sale type
        m = SALE_TYPE_RE.search(text)
        if m and not notice.sale_type:
            notice.sale_type = m.group(1).lower().strip()

    # Subtype: probate_heirs_notice — "NOTICE TO: NAME1, NAME2, NAME3..."
    elif PROBATE_HEIRS_NOTICE_RE.search(text):
        notice.notice_subtype = "probate_heirs_notice"
        m = PROBATE_HEIRS_NOTICE_RE.search(text)
        if m and not notice.heirs_named_in_notice:
            raw_list = m.group(1)
            # Split on commas, "and", "&"; trim; drop trailers like "AND TO WHOM IT MAY CONCERN"
            parts = re.split(r"\s*(?:,|\sand\s|&)\s*", raw_list, flags=re.IGNORECASE)
            heirs = []
            for p in parts:
                p = p.strip(" ,;.").rstrip(":")
                if not p or len(p) < 4:
                    continue
                # Drop catch-all phrases
                low = p.lower()
                if any(s in low for s in ("whom it may concern", "to all", "next of kin")):
                    continue
                # Title-case if all caps
                heirs.append(p.title() if p.isupper() else p)
            if heirs:
                notice.heirs_named_in_notice = " | ".join(heirs[:10])  # cap at 10

    else:
        notice.notice_subtype = "probate_creditors"

    # Co-PR detection — applies to any subtype. Independent of whether
    # owner_name has already been extracted.
    if CO_PR_FLAG_RE.search(text) and not notice.co_pr_names:
        # Best-effort: look for "NAME1 and NAME2" near the title
        co_pr_match = re.search(
            r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})"
            r"\s+and\s+"
            r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})"
            r"\s*,?\s*Co[-\s]?(?:Personal\s+Representatives?|Executors?)",
            text,
        )
        if co_pr_match:
            n1 = _clean_name(co_pr_match.group(1))
            n2 = _clean_name(co_pr_match.group(2))
            if _is_valid_name(n1) and _is_valid_name(n2):
                notice.co_pr_names = f"{n1} | {n2}"
                # If owner_name was empty or generic, populate from first co-PR
                if not notice.owner_name or not _is_valid_name(notice.owner_name):
                    notice.owner_name = n1


def _parse_pr_address(notice: NoticeData) -> None:
    """Extract the PR's mailing address from probate notice text.

    Probate notices contain the PR/Executor's mailing address (where creditors
    send claims), but NOT the decedent's property address. This extracts the
    PR's street, city, state, and zip into the owner_* fields.

    Tries the inline TN format first (PR_ADDRESS_RE — anchored on title with
    name+colon between), then falls back to the AL signature-block format
    (PR_ADDRESS_NAME_FIRST_RE — name on prior line, address right after
    title with minimal separator). Either match populates the owner_* slots.
    """
    if notice.notice_type != "probate":
        return

    text = notice.raw_text.replace("\xa0", " ")
    match = PR_ADDRESS_RE.search(text) or PR_ADDRESS_NAME_FIRST_RE.search(text)
    if match:
        street = _clean_address(match.group(1))
        # Title-case — PR addresses in notices are usually ALL CAPS
        if street.isupper():
            street = street.title()
        notice.owner_street = street
        notice.owner_city = _clean_city(match.group(2))
        # The PR_ADDRESS_RE regex matches "TN|AL" between city and zip
        # but DOESN'T capture the state literal. We re-extract it from
        # the matched span and validate against source text — handles
        # legitimate out-of-state PRs (Knoxville TN executor on an AL
        # decedent) while catching corrupted text where state and city
        # disagree.
        from state_resolver import state_for_county, validate_person_state
        # Re-scan the matched window for the state literal so we know
        # what the regex actually matched (TN or AL).
        import re as _re_local
        _matched_span = text[match.start():match.end()]
        _state_match = _re_local.search(
            r"\b(Tennessee|Tenn\.?|TN|Alabama|Ala\.?|AL)\b",
            _matched_span,
            _re_local.IGNORECASE,
        )
        _raw_state = _state_match.group(1) if _state_match else ""
        property_state = state_for_county(notice.county)
        notice.owner_state = validate_person_state(
            _raw_state,
            notice.raw_text,
            fallback_state=property_state,
        )
        notice.owner_zip = match.group(3)
        logger.debug(
            "PR address: %s, %s, %s %s",
            notice.owner_street, notice.owner_city, notice.owner_state, notice.owner_zip,
        )


def _clean_name(raw: str) -> str:
    """Normalize a name: trim, title-case, remove trailing conjunctions."""
    name = re.sub(r"\s+", " ", raw).strip()
    # Remove trailing "And" / "and" (word-level — don't strip from "Bolland" etc.)
    name = re.sub(r"\s+,?\s*(?:AND|and)\s*$", "", name)
    # Remove trailing commas, periods
    name = name.rstrip(",. ")
    # Title-case
    name = name.title()
    return name


# Entity-name acronyms that should stay uppercase after title-casing
_ENTITY_ACRONYMS = {
    "Llc": "LLC", "Lp": "LP", "Llp": "LLP", "Pllc": "PLLC",
    "Inc": "Inc", "Corp": "Corp", "Co": "Co",
    "Pa": "P.A.", "Pc": "P.C.",
    "Na": "N.A.", "Fsb": "F.S.B.", "Usa": "USA",
    "Mers": "MERS",
}


def _clean_entity_name(raw: str) -> str:
    """Normalize an entity/firm name preserving common acronyms (LLC, P.A., MERS).

    Used for mortgage company / trustee / original lender — title-casing alone
    would write "LLC" → "Llc". This keeps those tokens uppercase.
    """
    name = re.sub(r"\s+", " ", raw).strip()
    name = name.rstrip(",; ")
    name = name.title()
    # Restore acronyms — token-level so "Inca" doesn't get touched
    tokens = name.split(" ")
    for i, tok in enumerate(tokens):
        bare = tok.rstrip(",.")
        if bare in _ENTITY_ACRONYMS:
            tokens[i] = _ENTITY_ACRONYMS[bare] + tok[len(bare):]
    return " ".join(tokens)
