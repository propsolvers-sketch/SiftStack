"""Configuration for SiftStack — full-stack REI operations platform."""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
# Sub-divisions of output/ so the directory doesn't become a junk drawer.
# LEADS_DIR  — every datasift_upload_*.csv (DM / Heirs / per-distressor)
# DEALS_DIR  — deal-analyzer XLSX reports (MAO / ARV / financing scenarios)
# RAW_DIR    — al_notices_*.csv legacy backup dump + any forensic raw exports
# REPORTS_DIR — per-record deep-prospecting PDFs (already in use)
LEADS_DIR = OUTPUT_DIR / "leads"
DEALS_DIR = OUTPUT_DIR / "deals"
RAW_DIR = OUTPUT_DIR / "raw"
REPORTS_DIR = OUTPUT_DIR / "reports"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = PROJECT_ROOT / "last_run.json"
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"
SEEN_IDS_PRUNE_DAYS = 90
# Notices that exhausted all CAPTCHA retries during scraping.
# Persisted so the next run's summary can surface them instead of
# silently dropping — and a future retry pass can prioritize them.
CAPTCHA_FAILED_IDS_FILE = PROJECT_ROOT / "captcha_failed_ids.json"
CAPTCHA_FAILED_PRUNE_DAYS = 14
COOKIES_FILE = PROJECT_ROOT / "cookies.json"
DROPBOX_STATE_FILE = PROJECT_ROOT / "dropbox_state.json"
PHOTO_STATE_FILE = PROJECT_ROOT / "photo_state.json"

# ── Dropbox Watcher ────────────────────────────────────────────────────
DROPBOX_POLL_INTERVAL = int(os.getenv("DROPBOX_POLL_INTERVAL", "900"))  # seconds (default 15 min)
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "")  # root folder path in Dropbox, e.g. "/TN Public Notice"
DROPBOX_STORAGE_WARN_PERCENT = 80  # warn when storage usage exceeds this %

OUTPUT_DIR.mkdir(exist_ok=True)
LEADS_DIR.mkdir(exist_ok=True)
DEALS_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Credentials ────────────────────────────────────────────────────────
# Alabama Public Notices — no login required for search; CAPTCHA on detail pages only
APNA_EMAIL = os.getenv("APNA_EMAIL", "")    # Reserved for future Smart Search login
APNA_PASSWORD = os.getenv("APNA_PASSWORD", "")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")  # 2Captcha API key
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Haiku for LLM parsing
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")        # Smarty address standardization
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
OPENWEBNINJA_API_KEY = os.getenv("OPENWEBNINJA_API_KEY", "")  # Zillow property enrichment
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")              # Serper.dev Google Search API
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")        # Firecrawl JS-rendered scraping
TRACERFY_API_KEY = os.getenv("TRACERFY_API_KEY", "")          # Tracerfy skip tracing
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")            # Trestle phone validation
DATASIFT_EMAIL = os.getenv("DATASIFT_EMAIL", "")              # DataSift.ai login
DATASIFT_PASSWORD = os.getenv("DATASIFT_PASSWORD", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")        # Slack/Discord webhook
ANCESTRY_EMAIL = os.getenv("ANCESTRY_EMAIL", "")              # Ancestry.com login
ANCESTRY_PASSWORD = os.getenv("ANCESTRY_PASSWORD", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")            # Dropbox OAuth2 app key
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")
ZILLOW_PROXY_URL = os.getenv("ZILLOW_PROXY_URL", "")           # Webshare residential proxy for Zillow pending-listings scrape

# ── LLM Backend ──────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")           # "anthropic", "ollama", or "openrouter"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")  # Anthropic model name
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")        # Local Ollama model
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")       # OpenRouter API key
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# ── Site URLs ──────────────────────────────────────────────────────────
BASE_URL = "https://www.alabamapublicnotices.com"
SEARCH_URL = f"{BASE_URL}/Search.aspx"

# ── ASP.NET Selectors ─────────────────────────────────────────────────
# Search form (Alabama Public Notices — no login required)
SEL_SEARCH_TEXT = "#ctl00_ContentPlaceHolder1_as1_txtSearch"
SEL_SEARCH_TYPE_AND = "#ctl00_ContentPlaceHolder1_as1_rdoType_0"  # All Words
SEL_SEARCH_TYPE_OR = "#ctl00_ContentPlaceHolder1_as1_rdoType_1"   # Any Words
SEL_SEARCH_EXCLUDE = "#ctl00_ContentPlaceHolder1_as1_txtExclude"
SEL_SEARCH_DAYS = "#ctl00_ContentPlaceHolder1_as1_txtLastNumDays"
SEL_SEARCH_SUBMIT = "#ctl00_ContentPlaceHolder1_as1_btnGo"

# Search results grid
SEL_RESULTS_GRID = "#ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1"
SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'
SEL_VIEW_BUTTON_PATTERN = "input.viewButton"
SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
SEL_PAGE_INFO = "span[id$='lblTotalPages']"

# Notice detail page (DetailsPrint.aspx — reCAPTCHA + Terms gated)
SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"
RECAPTCHA_SITEKEY = "6LccnQ8sAAAAAMNFrb4ZLDtPAqk50k_r-CCwimHJ"

# ── Rate Limiting ──────────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0  # seconds between requests
REQUEST_DELAY_MAX = 3.0
MAX_RETRIES = 3
RESULTS_PER_PAGE = 50  # max the site allows

# ── Image Processing ───────────────────────────────────────────────────
BLUR_THRESHOLD = int(os.getenv("BLUR_THRESHOLD", "100"))   # Laplacian variance; below = rejected as blurry
TESSERACT_PSM_PDF = 3    # fully automatic — best for PDF tax sale tables
TESSERACT_PSM_PHOTO = 4  # assume single column of variable-size text — best for terminal screen photos

# ── Notice Types ───────────────────────────────────────────────────────
NOTICE_TYPES = ["foreclosure"]


@dataclass
class SearchConfig:
    """Represents a keyword search on alabamapublicnotices.com."""
    county: str
    notice_type: str    # One of NOTICE_TYPES
    search_terms: str   # Keywords for the search box
    search_type: str    # "AND" (all words) or "OR" (any words)
    exclude_terms: str  # Keywords to exclude
    days_back: int      # "Last N days" date range
    # Optional pre-classification — when set, every notice from this search
    # gets `notice.notice_subtype = <value>`. Used today for code-violation
    # searches to flag CONDEMNATION/DEMOLITION as "unsafe_building" so the
    # DataSift formatter fires the `demolish` tag automatically.
    notice_subtype: str = ""


# Keep SavedSearch as alias so non-scraper imports don't break
SavedSearch = SearchConfig

# ── Searches ───────────────────────────────────────────────────────────
# Jefferson County, AL — mortgage foreclosure sales + rescheduled notices
# Madison County, AL — newspaper PDF scans; detail pages use image-based PDF display
SAVED_SEARCHES: list[SearchConfig] = [
    SearchConfig(
        county="Jefferson",
        notice_type="foreclosure",
        search_terms="MORTGAGE FORECLOSURE SALE JEFFERSON",
        search_type="AND",
        exclude_terms="CONDEMNATION",
        days_back=7,
    ),
    SearchConfig(
        county="Jefferson",
        notice_type="foreclosure",
        search_terms="NOTICE OF MORTGAGE RESCHEDULE JEFFERSON",
        search_type="AND",
        exclude_terms="CONDEMNATION",
        days_back=7,
    ),
    SearchConfig(
        county="Madison",
        notice_type="foreclosure",
        search_terms="MORTGAGE FORECLOSURE SALE MADISON",
        search_type="AND",
        exclude_terms="CONDEMNATION",
        days_back=7,
    ),
    SearchConfig(
        county="Madison",
        notice_type="foreclosure",
        search_terms="NOTICE OF MORTGAGE RESCHEDULE MADISON",
        search_type="AND",
        exclude_terms="CONDEMNATION",
        days_back=7,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="foreclosure",
        search_terms="MORTGAGE FORECLOSURE SALE MARSHALL",
        search_type="AND",
        exclude_terms="CONDEMNATION",
        days_back=7,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="foreclosure",
        search_terms="NOTICE OF MORTGAGE RESCHEDULE MARSHALL",
        search_type="AND",
        exclude_terms="CONDEMNATION",
        days_back=7,
    ),
    # Probate "Notice to Creditors" publications. APN search has no county
    # filter — the county-of-property check happens in is_target_county()
    # against the full notice text after CAPTCHA.
    SearchConfig(
        county="Jefferson",
        notice_type="probate",
        search_terms="Estate Deceased",
        search_type="AND",
        exclude_terms="foreclosure mortgage",
        days_back=7,
    ),
    SearchConfig(
        county="Madison",
        notice_type="probate",
        search_terms="Estate Deceased",
        search_type="AND",
        exclude_terms="foreclosure mortgage",
        days_back=7,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="probate",
        search_terms="Estate Deceased",
        search_type="AND",
        exclude_terms="foreclosure mortgage",
        days_back=7,
    ),
    # Pre-probate via APN — formal "Notice of Death" publications.
    # Funeral homes and family members occasionally publish a Death Notice
    # as a legal notice (parallel to Notice-to-Creditors but BEFORE probate
    # is filed) to flush out unknown heirs, clear creditor claims for
    # non-probate estates, or for unclaimed-asset purposes. These hits
    # complement the obituary-driven pre-probate pipeline by catching
    # decedents whose families used a legal-notice channel instead of (or
    # in addition to) legacy.com / al.com / a funeral-home website.
    # Tagged as notice_type="pre_probate" so the existing pre-probate
    # tier-gate + Slack format picks them up. notice_subtype distinguishes
    # APN-sourced entries from obituary-harvested ones.
    SearchConfig(
        county="Jefferson",
        notice_type="pre_probate",
        notice_subtype="apn_death_notice",
        search_terms="Notice of Death",
        search_type="AND",
        # Exclude probate-court-driven publications (already covered above)
        # plus foreclosure boilerplate that occasionally mentions "death".
        exclude_terms="Estate Deceased foreclosure",
        days_back=7,
    ),
    SearchConfig(
        county="Madison",
        notice_type="pre_probate",
        notice_subtype="apn_death_notice",
        search_terms="Notice of Death",
        search_type="AND",
        exclude_terms="Estate Deceased foreclosure",
        days_back=7,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="pre_probate",
        notice_subtype="apn_death_notice",
        search_terms="Notice of Death",
        search_type="AND",
        exclude_terms="Estate Deceased foreclosure",
        days_back=7,
    ),
    # Alt phrasing — some publications use "Death Notice" or boilerplate
    # like "of the death of" instead of the strict "Notice of Death".
    SearchConfig(
        county="Jefferson",
        notice_type="pre_probate",
        notice_subtype="apn_death_notice",
        search_terms="Death Notice deceased",
        search_type="AND",
        exclude_terms="Estate Deceased foreclosure mortgage",
        days_back=7,
    ),
    SearchConfig(
        county="Madison",
        notice_type="pre_probate",
        notice_subtype="apn_death_notice",
        search_terms="Death Notice deceased",
        search_type="AND",
        exclude_terms="Estate Deceased foreclosure mortgage",
        days_back=7,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="pre_probate",
        notice_subtype="apn_death_notice",
        search_terms="Death Notice deceased",
        search_type="AND",
        exclude_terms="Estate Deceased foreclosure mortgage",
        days_back=7,
    ),
    # Code-violation / unsafe-building publications.
    #
    # Empirical recon (April 2026) showed naive keywords have severe
    # false-positive rates:
    #   CONDEMNATION    → catches drug/firearm forfeitures + ALDOT eminent
    #                     domain (NOT property teardowns)
    #   DEMOLITION      → catches construction bid solicitations
    #   PUBLIC NUISANCE → mixed; catches overgrown-grass soft violations
    #
    # The AL Code § 11-53A-20 boilerplate that real condemnation orders use
    # is the cleanest discriminator: every actual teardown notice cites it
    # verbatim ("pursuant to Sections 11-53A-20"). We use that as the AND
    # anchor + the action keyword, which drops the false-positive rate to
    # near zero. Madison/Jefferson hits will be rare regardless (Birmingham
    # uses 311; Huntsville publishes its own list — already covered in
    # Phase 1) but the filter is at least clean when something does land.
    SearchConfig(
        county="Jefferson",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="DEMOLITION UNSAFE STRUCTURE",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Jefferson",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="CONDEMNED STRUCTURE DEMOLITION",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Jefferson",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="NUISANCE ABATEMENT DEMOLISHED",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Madison",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="DEMOLITION UNSAFE STRUCTURE",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Madison",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="CONDEMNED STRUCTURE DEMOLITION",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Madison",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="NUISANCE ABATEMENT DEMOLISHED",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="DEMOLITION UNSAFE STRUCTURE",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="CONDEMNED STRUCTURE DEMOLITION",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    SearchConfig(
        county="Marshall",
        notice_type="code_violation",
        notice_subtype="unsafe_building",
        search_terms="NUISANCE ABATEMENT DEMOLISHED",
        search_type="AND",
        exclude_terms="bid contractor sealed",
        days_back=14,
    ),
    # Eviction / unlawful-detainer publications.
    #
    # NOT WIRED — empirical recon (April 2026) confirmed APN does NOT
    # carry Alabama eviction publications in any meaningful volume. Four
    # independent keyword probes statewide over a full 12-month window
    # all returned ZERO results:
    #   "UNLAWFUL DETAINER JEFFERSON"  → 0 hits
    #   "UNLAWFUL DETAINER" (statewide) → 0 hits
    #   "DETAINER WARRANT" (statewide)  → 0 hits
    #   "EVICTION TENANT" (statewide)   → 0 hits
    #   "LANDLORD TENANT NOTICE"        → 0 hits
    #
    # Why: unlike foreclosures (§ 35-10-13 mandates publication of trustee
    # sales) and probate creditor notices (§ 43-2-61 mandates publication
    # for 3 successive weeks), Alabama eviction statute § 6-6-310 et seq.
    # requires personal service, not publication. ARCP Rule 4.3 allows
    # service by publication when a tenant can't be located, but landlords
    # rarely use it — they take default judgment after the 14-day response
    # window instead, since publication costs them money and the tenant
    # has typically already abandoned the property anyway.
    #
    # Coverage paths for evictions:
    #   1. AlaCourt.com subscription (~$100/yr + per-record fees) —
    #      structured statewide unlawful-detainer search, full coverage.
    #      Requires Playwright scrape behind login.
    #   2. County district court online dockets (Jefferson 10th Circuit /
    #      Madison 23rd Circuit) — free but PDF-format and inconsistent.
    #   3. Photo-import pipeline (already wired in photo_importer.py)
    #      — operator photographs courthouse terminal, OCR + LLM parse.
    #
    # The eviction LLM prompt + result handler ARE wired (llm_parser.py,
    # notice_parser.py:1015-1048) so any future eviction-source adapter
    # plugs into the existing NoticeData → DataSift pipeline cleanly.
    # Just need a different upstream feed than APN.
    #
    # ─────────────────────────────────────────────────────────────────
    # Tax sale (delinquent-property auction) publications.
    #
    # NOT WIRED — recon not performed. Distinct from tax_delinquent
    # (already covered for Jefferson + Madison via the dedicated
    # tax-collector adapters in tax_distress_pipeline.py): tax_sale
    # is the actual auction event, tax_delinquent is the upstream
    # signal. They overlap in prospecting value — most properties
    # that hit a tax_sale were tax_delinquent for years first, so
    # the existing pipeline catches them earlier. The marginal
    # value-add of wiring tax_sale is the firm auction date for
    # bidders, not new lead discovery.
    #
    # Statutory basis for publication exists: AL Code § 40-10-184
    # requires the tax collector to publish the sale list once a
    # week for two consecutive weeks before the sale. Whether that
    # publication surfaces on APN vs. only in print/county sites
    # is unverified — needs empirical recon.
    #
    # Coverage paths to investigate (in priority order):
    #   1. APN keyword search — try "TAX SALE JEFFERSON" /
    #      "TAX SALE MADISON" / "DELINQUENT TAX SALE" with
    #      days_back=30 (annual sales are typically May/June, so
    #      a daily run will see them only in season). Wire
    #      SearchConfigs the same way as the foreclosure entries
    #      if results materialize.
    #   2. County tax-collector publication endpoints directly
    #      (Jefferson Co Revenue Commission / Madison Co Tax
    #      Collector) — both publish annual tax-sale lists on
    #      their own sites. More reliable than APN since the
    #      publication duty is on the county, not a private seller.
    #
    # notice_parser.py + llm_parser.py already handle tax_sale as a
    # distinct notice_type (legacy from the TN scraper era). Wiring
    # an AL source means SearchConfig entries OR a county-tax-collector
    # adapter in the shape of madison_tax_delinquent_api.py — no
    # parser/formatter changes needed.
    #
    # ─────────────────────────────────────────────────────────────────
    # Divorce / dissolution-of-marriage publications.
    #
    # NOT WIRED — recon not performed, AND not currently parsed.
    # Two-layer gap: no upstream feed AND notice_parser.py does not
    # handle divorce as a notice_type today (only photo_importer.py
    # does, for courthouse-terminal photo imports).
    #
    # Like eviction, AL divorce procedure (§ 30-2-1 et seq.) requires
    # personal service in most cases. ARCP Rule 4.3 allows service
    # by publication when the respondent can't be located (typically:
    # spouse who left and can't be tracked down). Volume is likely
    # LOW — most divorces are uncontested and personally served.
    #
    # Strategic priority is also low: divorce-as-distress signal
    # works best when the property is ALSO in foreclosure (which the
    # foreclosure pipeline catches independently) or when the court
    # has ordered a sale (which would surface as a probate_sale-style
    # notice). A divorce alone, without financial distress, is a
    # weak motivated-seller signal.
    #
    # Coverage paths if revisited:
    #   1. APN keyword search — "DIVORCE NOTICE PUBLICATION" /
    #      "DISSOLUTION OF MARRIAGE BY PUBLICATION".
    #   2. AlaCourt domestic-relations docket subscription (same
    #      source proposed for evictions above).
    #   3. Photo-import pipeline (already handles divorce — see
    #      photo_importer.py — for courthouse terminal scans).
    #
    # Wiring end-to-end requires more work than tax_sale: NoticeData
    # fields for petitioner/respondent (currently in photo_importer.py
    # only), an LLM prompt branch in llm_parser.py, and a regex parser
    # in notice_parser.py. Estimate 1-2 days to bring divorce parity
    # with the other notice types. Defer until APN recon shows
    # non-trivial volume to justify the investment.
]

# ── Entity Detection ──────────────────────────────────────────────────
# Business entity patterns — shared across obituary_enricher, tax_enricher,
# and enrichment_pipeline for entity filtering.
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|"
    r"PARTNERSHIP|ASSOCIATION|ASSOC|BANK|CREDIT UNION|CHURCH|MINISTRIES|"
    r"HOUSING|AUTHORITY|DEVELOPMENT|ENTERPRISES|PROPERTIES|INVESTMENTS|"
    r"GROUP|HOLDINGS|MANAGEMENT|SERVICES|FOUNDATION|ORGANIZATION)\b",
    re.IGNORECASE,
)

# Trust/estate patterns — personal trusts are NOT business entities
TRUST_NAME_RE = re.compile(
    r"^(?:THE\s+)?([\w]+(?:\s+[\w.]+)+?)\s+(?:REVOCABLE\s+)?(?:LIVING\s+)?TRUST\b",
    re.IGNORECASE,
)
ESTATE_OF_RE = re.compile(
    r"^(?:THE\s+)?ESTATE\s+OF\s+([\w]+(?:\s+[\w.]+)+?)(?:\s*,|\s*$)",
    re.IGNORECASE,
)

_config_logger = logging.getLogger(__name__)


# ── State File Utilities ─────────────────────────────────────────────


def save_state(path: Path, data: dict) -> None:
    """Write JSON state to disk atomically (write tmp → rename).

    Creates a .bak copy of the previous file before overwriting.
    """
    # Back up current file
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass  # Best-effort backup

    # Atomic write: tmp → rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _config_logger.warning("Failed to read %s: %s", candidate, e)
    return {}
