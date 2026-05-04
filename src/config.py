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
