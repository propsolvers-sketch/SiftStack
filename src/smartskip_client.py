"""SmartSkip Playwright client — submit CSV batches, poll for completion, download results.

SmartSkip has no public API. This module drives the app.smartskip.io web UI
via a persistent-context Playwright browser so future runs reuse the operator
session cookies stored in .smartskip_profile/.

Wizard flow (verified via UI recon 2026-08-04):

  Step 1 — Upload .csv:  input[type=file], drag/drop zone
  Step 2 — Map columns:  drag from left "Columns from .csv" chips to right
                         "SmartSkip Fields" slots
                         Required: FIRST NAME, LAST NAME, MAILING ADDRESS
                         Optional: MIDDLE NAME, MAILING CITY/STATE/ZIP,
                                   PROPERTY ADDRESS/CITY/STATE/ZIP
  Step 3 — Preview:      table showing mapped columns; click Next
  Step 4 — Payment:      shows row count, duplicates, cost/row, total.
                         Debits account balance (top-right). Click confirm.
  Step 5 — Finish:       "Your file is being processed" — email sent when done
                         but results also appear in Skips History on the
                         Bulk Skip landing page (observed: minutes-to-complete
                         for 4-row test batch)

Skips History table (on Bulk Skip landing):
  File name · Date of search · Expiration date (60 days) · Status
  (Processing / Completed) · Results (count) · Download button

Result CSV shape (200+ columns):
  Owner: First/Last/Middle Name, Mailing + Property address, Age, Deceased,
         Phone 1-7 (number/type/connected), Email 1-2
  Relative 1-14: First/Last, Possible Type (Spouse/Child/In-law/Other Relative/
                 Coworker/Friend/Past neighbor), Age, Mailing address, Phone 1-5,
                 Email 1-5
  Associate 1-5: same shape as relatives (Past neighbor, Coworker typically)

Cost: $0.15 / row, $0.50 minimum per batch (kicks in only < 4 rows).

Selectors below are the best-guess from recon screenshots — some may need
tightening on first live-run. Where fragile, we've used semantic matches
(text content, aria labels) over CSS classes.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

SMARTSKIP_BASE_URL = "https://app.smartskip.io"
BULK_SKIP_URL      = f"{SMARTSKIP_BASE_URL}/bulk-skip"
LOGIN_URL          = f"{SMARTSKIP_BASE_URL}/login"

# Persistent profile so session cookies survive between runs
DEFAULT_PROFILE_DIR = Path.home() / "Desktop" / "SiftStack" / ".smartskip_profile"

# Wait tuning
UPLOAD_TIMEOUT_S   = 60         # file upload
STEP_ADVANCE_S     = 15         # each Next / Confirm click
PROCESSING_POLL_S  = 15         # gap between status polls
PROCESSING_MAX_S   = 900        # 15 min max wait for a batch to complete

# CSV column headers SmartSkip's mapping wizard expects on the LEFT
# ("Columns from .csv"). Only these will map cleanly to the RIGHT
# ("SmartSkip Fields"). Additional columns are tolerated but ignored.
#
# Property address fields intentionally EXCLUDED per operator directive
# (2026-08-06): for probate records the PR/executor often does not live at
# the decedent's property, so property address is uncorrelated with the
# seller we're actually trying to locate. Sending it confuses SmartSkip's
# matching algorithm. Mailing address is the definitive locator.
SUBMISSION_COLUMNS = [
    "First Name", "Last Name", "Middle Name",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
]

# Required fields — SmartSkip won't advance past Map without these
REQUIRED_COLUMNS = {"First Name", "Last Name", "Mailing Address"}


# ─────────────────────────────────────────────────────────────────────
# Dataclasses — inputs + outputs
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SmartSkipRow:
    """One row to submit to SmartSkip. All fields optional except the required 3."""
    first_name:      str
    last_name:       str
    mailing_address: str
    middle_name:     str = ""
    mailing_city:    str = ""
    mailing_state:   str = ""
    mailing_zip:     str = ""
    property_address: str = ""
    property_city:    str = ""
    property_state:   str = ""
    property_zip:     str = ""
    # Free-form correlation key so caller can map results back to their record.
    # NOT sent to SmartSkip — kept only in the pre-submit metadata store.
    external_id:     str = ""

    def to_csv_row(self) -> dict[str, str]:
        return {
            "First Name":       self.first_name,
            "Last Name":        self.last_name,
            "Middle Name":      self.middle_name,
            "Mailing Address":  self.mailing_address,
            "Mailing City":     self.mailing_city,
            "Mailing State":    self.mailing_state,
            "Mailing Zip":      self.mailing_zip,
            "Property Address": self.property_address,
            "Property City":    self.property_city,
            "Property State":   self.property_state,
            "Property Zip":     self.property_zip,
        }


@dataclass
class SmartSkipContact:
    """Contact info for owner OR a relative/associate."""
    role: str                       # "owner" | "Spouse" | "Child" | "In-law" | etc.
    first_name: str = ""
    last_name: str = ""
    age: int | None = None
    deceased: bool = False          # from SmartSkip's Deceased column
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phones: list[dict[str, str]] = field(default_factory=list)   # [{number, type, connected}]
    emails: list[str] = field(default_factory=list)

    @property
    def is_signing_authority(self) -> bool:
        """Signing chain per AL intestate succession law."""
        return self.role in {"Spouse", "Child", "In-law"}


@dataclass
class SmartSkipResult:
    """Parsed result for one input row."""
    row_index: int
    owner: SmartSkipContact
    relatives: list[SmartSkipContact] = field(default_factory=list)   # up to 14
    associates: list[SmartSkipContact] = field(default_factory=list)  # up to 5

    @property
    def has_data(self) -> bool:
        """Did SmartSkip return anything actionable for this row?"""
        return bool(
            self.owner.phones or self.owner.emails
            or self.relatives or self.associates
        )

    @property
    def heirs(self) -> list[SmartSkipContact]:
        """Relatives with signing authority (Spouse / Child / In-law)."""
        return [r for r in self.relatives if r.is_signing_authority]


@dataclass
class SmartSkipBatch:
    """Metadata for one submitted batch — persisted so downloader can find it."""
    batch_id: str              # filename we uploaded (used as key in Skips History)
    row_count: int
    submitted_at: str          # ISO timestamp
    csv_path: Path             # local copy of what we uploaded
    external_ids: list[str]    # row-index → operator-side record UUID


# ─────────────────────────────────────────────────────────────────────
# CSV builder + parser
# ─────────────────────────────────────────────────────────────────────


def write_submission_csv(rows: list[SmartSkipRow], out_path: Path) -> None:
    """Serialize rows in the exact column order SmartSkip's mapper expects."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUBMISSION_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_row())


def _parse_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _extract_phones(row: dict[str, str], prefix: str, max_slots: int) -> list[dict[str, str]]:
    """Pull Phone 1..N columns for a given prefix ('' for owner, 'RELATIVE 1: ', etc)."""
    phones = []
    for i in range(1, max_slots + 1):
        num_key = f"{prefix}Phone {i} number" if prefix else f"Phone {i} number"
        type_key = f"{prefix}Phone {i} type" if prefix else f"Phone {i} type"
        conn_key = f"{prefix}Phone {i} connected" if prefix else f"Phone {i} connected"
        num = (row.get(num_key) or "").strip()
        if not num:
            continue
        phones.append({
            "number": num,
            "type": (row.get(type_key) or "").strip(),
            "connected": (row.get(conn_key) or "").strip(),
        })
    return phones


def _extract_emails(row: dict[str, str], prefix: str, max_slots: int) -> list[str]:
    emails = []
    for i in range(1, max_slots + 1):
        key = f"{prefix}Email {i}" if prefix else f"Email {i}"
        val = (row.get(key) or "").strip()
        if val:
            emails.append(val)
    return emails


def parse_result_csv(csv_path: Path) -> Iterator[SmartSkipResult]:
    """Yield one SmartSkipResult per input row."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            owner = SmartSkipContact(
                role="owner",
                first_name=(row.get("First Name") or "").strip(),
                last_name=(row.get("Last Name") or "").strip(),
                age=_parse_int(row.get("Age", "")),
                deceased=(row.get("Deceased") or "").strip().lower() in
                          ("yes", "true", "1", "y"),
                address=(row.get("Mailing Address") or "").strip(),
                city=(row.get("Mailing City") or "").strip(),
                state=(row.get("Mailing State") or "").strip(),
                zip=(row.get("Mailing Zip") or "").strip(),
                phones=_extract_phones(row, "", 7),
                emails=_extract_emails(row, "", 2),
            )
            relatives = []
            for r in range(1, 15):   # RELATIVE 1..14
                prefix = f"RELATIVE {r}: "
                first = (row.get(f"{prefix}First Name") or "").strip()
                last = (row.get(f"{prefix}Last Name") or "").strip()
                if not (first or last):
                    continue
                relatives.append(SmartSkipContact(
                    role=(row.get(f"{prefix}Possible Type") or "").strip() or "Unknown",
                    first_name=first,
                    last_name=last,
                    age=_parse_int(row.get(f"{prefix}Age", "")),
                    address=(row.get(f"{prefix}Mailing Street") or "").strip(),
                    city=(row.get(f"{prefix}Mailing City") or "").strip(),
                    state=(row.get(f"{prefix}Mailing State") or "").strip(),
                    zip=(row.get(f"{prefix}Mailing ZIP Code") or "").strip(),
                    phones=_extract_phones(row, prefix, 5),
                    emails=_extract_emails(row, prefix, 5),
                ))
            associates = []
            for a in range(1, 6):    # ASSOCIATE 1..5
                prefix = f"ASSOCIATE {a}: "
                first = (row.get(f"{prefix}First Name") or "").strip()
                last = (row.get(f"{prefix}Last Name") or "").strip()
                if not (first or last):
                    continue
                associates.append(SmartSkipContact(
                    role=(row.get(f"{prefix}Possible Type") or "").strip() or "Associate",
                    first_name=first, last_name=last,
                    age=_parse_int(row.get(f"{prefix}Age", "")),
                    address=(row.get(f"{prefix}Mailing Street") or "").strip(),
                    city=(row.get(f"{prefix}Mailing City") or "").strip(),
                    state=(row.get(f"{prefix}Mailing State") or "").strip(),
                    zip=(row.get(f"{prefix}Mailing ZIP Code") or "").strip(),
                    phones=_extract_phones(row, prefix, 5),
                    emails=_extract_emails(row, prefix, 5),
                ))
            yield SmartSkipResult(
                row_index=idx,
                owner=owner,
                relatives=relatives,
                associates=associates,
            )


# ─────────────────────────────────────────────────────────────────────
# Note formatter — the definitive DataSift Notes entry when SmartSkip matches
# ─────────────────────────────────────────────────────────────────────


def _fmt_phone_line(phone: dict, tier: str = "") -> str:
    """Format a single phone entry: '205-441-6822 (Mobile · Dial First)'.

    tier is optional — omitted if empty or 'Unknown'.
    """
    ptype = (phone.get("type") or "").strip() or "Phone"
    connected = phone.get("connected") == "connected"
    parts = [ptype]
    if not connected:
        parts.append("disconnected")
    if tier and tier != "Unknown":
        parts.append(tier)
    return f"    {phone['number']} ({' · '.join(parts)})"


def format_note(
    result: SmartSkipResult,
    scraped_date: str,
    phone_tiers: dict[str, str] | None = None,
    probate_context: dict | None = None,
    is_probate_record: bool = False,
) -> str:
    """Build the DataSift Notes entry for a matched SmartSkip result.

    Dense single-scroll format designed for operator readability during a
    live call. Owner + heirs get spelled-out phones (with Trestle tier if
    available); extended family stays compact (info-only, not in call flow).

    phone_tiers: optional {phone_number: tier} map from Trestle scoring.
        e.g. {"2054416822": "Dial First", "2053357413": "Dial Second"}
        When provided, tier appears next to each phone.
    """
    if not result.has_data:
        return ""

    tiers = phone_tiers or {}

    def tier_of(number: str) -> str:
        # Normalize to digits-only for lookup (handles both "205-441-6822" and "2054416822")
        digits = "".join(c for c in number if c.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return tiers.get(digits, tiers.get(number, ""))

    lines: list[str] = []
    lines.append("═" * 55)
    lines.append(f"🥇 TOP-TIER · SmartSkip Family Tree · {scraped_date}")
    lines.append("Deepest heir/relative data — prioritize this note over other")
    lines.append("vendors' skip-trace summaries when calling.")
    lines.append("═" * 55)

    # ── Courthouse / Probate context (only if populated) ──
    # Pulled from DataSift record fields by the caller. Includes obituary +
    # court + PR + attorney info when present. Skipped entirely for records
    # with no probate metadata (bulk-purchased, non-probate records, etc.).
    if probate_context:
        ctx = probate_context
        court_lines = []
        if ctx.get("smartskip_deceased_flag"):
            court_lines.append(
                f"  ⚰  SmartSkip flags owner as DECEASED "
                f"(consumer-data source, not necessarily court-confirmed)"
            )
        if ctx.get("decedent_name"):
            court_lines.append(f"  Decedent:         {ctx['decedent_name']}")
        if ctx.get("last_obituary_date"):
            court_lines.append(f"  Obituary date:    {ctx['last_obituary_date']}")
        if ctx.get("obituary_url"):
            court_lines.append(f"  Obituary URL:     {ctx['obituary_url']}")
        if ctx.get("date_of_death"):
            court_lines.append(f"  Date of death:    {ctx['date_of_death']}")
        if ctx.get("probate_open_date"):
            court_lines.append(f"  Probate opened:   {ctx['probate_open_date']}")
        if ctx.get("probate_case_number"):
            court_lines.append(f"  Case #:           {ctx['probate_case_number']}")
        if ctx.get("judge_of_probate"):
            court_lines.append(f"  Judge:            {ctx['judge_of_probate']}")
        if ctx.get("personal_representative"):
            pr_line = f"  PR / Executor:    {ctx['personal_representative']}"
            if ctx.get("personal_representative_phone"):
                pr_line += f" · {ctx['personal_representative_phone']}"
            court_lines.append(pr_line)
        if ctx.get("attorney_on_file"):
            court_lines.append(f"  Attorney:         {ctx['attorney_on_file']}")
        if ctx.get("creditor_claim_deadline"):
            court_lines.append(f"  Creditor deadline: {ctx['creditor_claim_deadline']}")
        if ctx.get("hearing_date"):
            court_lines.append(f"  Hearing date:     {ctx['hearing_date']}")
        if ctx.get("notice_type") or ctx.get("county") or ctx.get("source_url"):
            src_bits = [b for b in (ctx.get("notice_type"), ctx.get("county")) if b]
            src_line = "  Source:           " + " · ".join(src_bits) if src_bits else "  Source:"
            if ctx.get("source_url"):
                src_line += f"\n                     {ctx['source_url']}"
            court_lines.append(src_line)

        if court_lines:
            lines.append("")
            lines.append("── 🏛 Courthouse / Probate Context (from record fields) ──")
            lines.extend(court_lines)
        elif is_probate_record:
            # Record is on Probate list but no courthouse metadata is on file.
            # Make the gap visible so operator knows source vs. bulk-list origin.
            lines.append("")
            lines.append("── 🏛 Courthouse / Probate Context ──")
            lines.append("  ⚠ No courthouse metadata on file for this record.")
            lines.append("  Record is on the Probate list but was added without probate")
            lines.append("  court source data (e.g. bulk list purchase, manual add).")
            lines.append("  Records from APN · AdHunter · Benchmark · Pre-Probate")
            lines.append("  pipelines will populate this section with case number, PR,")
            lines.append("  attorney, obituary URL, hearing date, etc.")
    elif is_probate_record:
        # No context object passed but caller says this is a probate record
        lines.append("")
        lines.append("── 🏛 Courthouse / Probate Context ──")
        lines.append("  ⚠ No courthouse metadata on file for this record.")

    # ── Owner ──
    o = result.owner
    age_str = f", age {o.age}" if o.age else ""
    lines.append(f"Owner: {o.first_name} {o.last_name}{age_str}")
    if o.phones:
        for p in o.phones:
            lines.append(_fmt_phone_line(p, tier_of(p["number"])))
    if o.emails:
        lines.append(f"  Emails: {', '.join(o.emails)}")
    if not o.phones and not o.emails:
        lines.append("  (no direct owner contact info)")

    # ── Heirs (signing authority — separate DataSift records) ──
    heirs = result.heirs
    if heirs:
        lines.append("")
        lines.append("── Heirs (created as separate DataSift records; tag: heir_of_probate) ──")
        for h in heirs:
            age = f", {h.age}" if h.age else ""
            location = f"{h.city} {h.state}".strip()
            lines.append(f"  {h.role}: {h.first_name} {h.last_name}{age} · {location}")
            for p in h.phones:
                lines.append(_fmt_phone_line(p, tier_of(p["number"])))
            if h.emails:
                lines.append(f"    Emails: {', '.join(h.emails)}")

    # ── Extended family (info only, not created as records) ──
    extended = [r for r in result.relatives if not r.is_signing_authority]
    if extended:
        lines.append("")
        lines.append("── Extended family (info only, no separate records) ──")
        for r in extended[:8]:   # cap to keep note readable
            age = f", {r.age}" if r.age else ""
            phones = f"{len(r.phones)}p" if r.phones else "0p"
            emails = f"/{len(r.emails)}e" if r.emails else ""
            lines.append(f"  {r.role}: {r.first_name} {r.last_name}{age} · {phones}{emails}")
        if len(extended) > 8:
            lines.append(f"  [+ {len(extended) - 8} more — see SmartSkip Records tab]")

    if result.associates:
        lines.append("")
        lines.append(f"── {len(result.associates)} associate(s) (past neighbors/coworkers) omitted ──")

    lines.append("═" * 55)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Playwright client
# ─────────────────────────────────────────────────────────────────────


class SmartSkipClient:
    """Persistent-context Playwright client for app.smartskip.io.

    Usage:

        with SmartSkipClient() as client:
            batch = client.submit_batch(rows, batch_label="Probate 2026-08-04")
            # ... time passes ...
            results = client.wait_and_download(batch)
            for result in parse_result_csv(results):
                if result.has_data:
                    note = format_note(result, batch.submitted_at)
                    ...
    """

    def __init__(
        self,
        *,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        download_dir: Path | None = None,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self.download_dir = download_dir or (Path.home() / "Desktop" / "SiftStack" / "outbox" / "smartskip")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None

    def __enter__(self) -> "SmartSkipClient":
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    @property
    def page(self) -> Page:
        assert self._ctx is not None, "SmartSkipClient must be used as a context manager"
        return self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _assert_logged_in(self) -> None:
        """Navigate to Bulk Skip; if we hit /login, session has expired."""
        self.page.goto(BULK_SKIP_URL, wait_until="domcontentloaded")
        if "/login" in self.page.url:
            raise SmartSkipSessionExpired(
                "SmartSkip session expired — operator must re-login in the "
                "persistent profile browser. Run scratch/smartskip_recon/"
                "launch_and_explore.py to re-authenticate."
            )

    def _assert_positive_balance(self, min_required_usd: float = 0.50) -> None:
        """Read the account balance widget in the top-right corner.

        SmartSkip shows the balance as an anchor/button with text like "$12.30"
        near the user avatar. If we can't parse it OR it's below the minimum,
        raise SmartSkipPaymentError before spending any time on the wizard.

        Discovered 2026-08-06: with $0 balance, the payment step silently
        opens a Stripe card-entry modal that our automation can't fill.
        Automation clicks "Next" past it → wizard appears to succeed but
        no batch is actually submitted (and no charge occurs).
        """
        self.page.wait_for_timeout(1000)
        balance_text = self.page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('a, button, span, div')) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (/^\\$\\d+(\\.\\d+)?$/.test(t)) return t;
                }
                return '';
            }
        """)
        if not balance_text:
            logger.warning("Could not read SmartSkip balance widget — proceeding anyway")
            return
        try:
            balance = float(balance_text.replace('$', '').strip())
        except ValueError:
            logger.warning("Could not parse balance %r — proceeding anyway", balance_text)
            return
        logger.info("SmartSkip account balance: %s", balance_text)
        if balance < min_required_usd:
            raise SmartSkipPaymentError(
                f"SmartSkip account balance is {balance_text}, below the "
                f"${min_required_usd:.2f} minimum needed. "
                f"Top up the account at app.smartskip.io/billing before running "
                f"probate_cascade.py again. No batch was submitted; no charge "
                f"occurred. Automated card-entry through the Payment step's "
                f"Stripe modal is NOT supported."
            )

    # ── Submit ────────────────────────────────────────────────────────

    def submit_batch(
        self,
        rows: list[SmartSkipRow],
        *,
        batch_label: str,
    ) -> SmartSkipBatch:
        """Full submit flow: write CSV → upload → map columns → pay → confirm.

        Returns SmartSkipBatch metadata that the consumer will use to find
        this batch in Skips History and download results.
        """
        if not rows:
            raise ValueError("submit_batch called with 0 rows")

        # 1. Write submission CSV to local outbox
        safe_label = batch_label.replace("/", "-").replace(" ", "_")
        submitted_at = time.strftime("%Y-%m-%d_%H%M%S")
        filename = f"{safe_label}_{submitted_at}.csv"
        csv_path = self.download_dir / "submitted" / filename
        write_submission_csv(rows, csv_path)
        logger.info("Wrote submission CSV: %s (%d rows)", csv_path, len(rows))

        # 2. Verify session + navigate. NOTE: SmartSkip Bulk Skip charges the
        # credit card on file, NOT the wallet balance (operator confirmed
        # 2026-08-06). So no balance check — just make sure we're logged in
        # + on a fresh Bulk Skip page (kills any stale wizard state from a
        # prior aborted run).
        self._assert_logged_in()
        # Force a fresh navigation to clear any cached in-progress wizard state
        self.page.goto(BULK_SKIP_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)

        # 3. Wizard steps 1-5
        self._wizard_step_1_upload(csv_path)
        self._wizard_step_2_map_columns()
        self._wizard_step_3_preview()
        self._wizard_step_4_payment()
        self._wizard_step_5_finish()

        return SmartSkipBatch(
            batch_id=filename,
            row_count=len(rows),
            submitted_at=submitted_at,
            csv_path=csv_path,
            external_ids=[r.external_id for r in rows],
        )

    def _wizard_step_1_upload(self, csv_path: Path) -> None:
        """Step 1: Feed the file to SmartSkip's drop-zone widget.

        SmartSkip's upload area is a drag-drop zone with the actual
        <input type=file> hidden behind JS (verified via live-run
        2026-08-05 — direct locator times out after 30s). Two strategies:

        1. Try direct set_input_files on the hidden input (works if the
           element is in the DOM but visually hidden — Playwright's
           set_input_files handles hidden inputs).
        2. Fall back to expect_file_chooser + click the "Choose .csv file"
           link — catches the file-picker dialog before the OS dialog
           actually appears.
        """
        logger.info("SmartSkip wizard step 1: upload %s", csv_path.name)

        # Strategy 1: Direct set_input_files with a short timeout so we
        # can fall through fast if the input doesn't exist in the DOM.
        try:
            file_input = self.page.locator("input[type=file]").first
            file_input.set_input_files(str(csv_path), timeout=5000)
            logger.debug("Uploaded via direct hidden-input set_input_files")
        except PWTimeout:
            # Strategy 2: Trigger the file chooser by clicking the visible
            # "Choose .csv file" link, and inject the file into the dialog.
            logger.debug("Direct input not found — falling back to file-chooser")
            with self.page.expect_file_chooser(timeout=UPLOAD_TIMEOUT_S * 1000) as fc_info:
                # The link text can be "Choose .csv file" or similar
                self.page.get_by_text("Choose", exact=False).first.click()
            file_chooser = fc_info.value
            file_chooser.set_files(str(csv_path))
            logger.debug("Uploaded via file-chooser fallback")

        # ── VERIFY upload actually took effect ──
        # Wizard stepper labels ("Map the columns", etc.) are ALWAYS visible
        # at the top so text-matching is unreliable. Poll for `.item-name`
        # chips appearing on the LEFT panel — those only exist after a
        # successful CSV upload.
        chip_check_deadline = time.time() + 15
        chip_count = 0
        while time.time() < chip_check_deadline:
            chip_count = self.page.locator('span.item-name').count()
            if chip_count > 0:
                break
            self.page.wait_for_timeout(500)

        if chip_count == 0:
            # Upload didn't create chips → SmartSkip rejected the file OR
            # our file-input selector targeted the wrong element.
            self._dump_mapping_dom("upload_did_not_create_chips")
            raise SmartSkipMappingError(
                f"CSV upload did not create any draggable chips after 15s. "
                f"SmartSkip likely rejected the file OR set_input_files hit "
                f"the wrong element. See DOM dump above + screenshot. "
                f"NOTHING was submitted; no charge occurred."
            )
        logger.info("  Upload confirmed: %d chips detected on Step 2", chip_count)

    def _click_wizard_action_button(self, action_labels: tuple[str, ...]) -> None:
        """Click a wizard action button (Next/Continue/Preview/Pay/Confirm/etc).

        Tolerant of Vuetify UI conventions:
        * Excludes `v-stepper-item` (disabled stepper dots at top of wizard)
        * Filters to enabled buttons only
        * Casts wide net across <button>, <a>, and [role=button] elements
        * On failure, dumps all enabled buttons + saves a screenshot for triage

        Raises SmartSkipMappingError with a diagnostic dump if nothing matches.
        """
        # Let any auto-mapping / state change settle
        self.page.wait_for_timeout(2500)

        # Cast a wide net: <button>, <a>, and [role=button] can all be action buttons
        # in Vuetify. Some are icon-only (aria-label). Try text match + aria-label
        # + partial match for prefixed labels like "Pay $0.60".
        for label in action_labels:
            selectors = [
                # Exact button text
                f'button:not(.v-stepper-item):not([disabled]):has-text("{label}")',
                # Anchor styled as button
                f'a.v-btn:not([disabled]):has-text("{label}")',
                # Any role=button
                f'[role="button"]:not([disabled]):has-text("{label}")',
                # aria-label based match
                f'button:not(.v-stepper-item):not([disabled])[aria-label*="{label}" i]',
                # Vuetify v-btn scoped
                f'.v-btn:not(.v-stepper-item):not(.v-btn--disabled):has-text("{label}")',
            ]
            for selector in selectors:
                loc = self.page.locator(selector).first
                try:
                    loc.wait_for(state="visible", timeout=2000)
                    loc.scroll_into_view_if_needed(timeout=2000)
                    loc.click(timeout=5000)
                    logger.debug("Clicked wizard action button via %r → %r",
                                 selector, label)
                    return
                except PWTimeout:
                    continue

        # ── JS-CLICK FALLBACK ─────────────────────────────────────────────
        # Playwright's locator matching can miss Vuetify buttons because the
        # visible text lives inside a nested <span class="v-btn__content">.
        # This bypass runs querySelectorAll in the page and clicks the first
        # enabled non-stepper button whose innerText matches any label. Works
        # even when Playwright's :has-text() misses due to shadow-DOM-like
        # text nesting.
        js_result = self.page.evaluate("""
            (labels) => {
                const candidates = document.querySelectorAll(
                    'button, a.v-btn, [role="button"], .v-btn'
                );
                for (const label of labels) {
                    for (const el of candidates) {
                        // Filter: disabled state
                        if (el.disabled) continue;
                        if (el.getAttribute('aria-disabled') === 'true') continue;
                        if (el.classList.contains('v-btn--disabled')) continue;
                        if (el.classList.contains('v-stepper-item')) continue;
                        // Filter: visibility (has bounding box)
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        // Match: innerText or aria-label
                        const text = (el.innerText || el.textContent || '').trim();
                        const aria = el.getAttribute('aria-label') || '';
                        if (text === label || text.startsWith(label + ' ') ||
                            aria.toLowerCase().includes(label.toLowerCase())) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return {clicked: true, label, text: text.slice(0, 60)};
                        }
                    }
                }
                return {clicked: false};
            }
        """, list(action_labels))
        if js_result.get("clicked"):
            logger.debug("JS-clicked wizard button: label=%r text=%r",
                         js_result["label"], js_result["text"])
            # Small wait for click event to propagate
            self.page.wait_for_timeout(500)
            return

        # ── Diagnostic dump: nothing matched, help the operator (or me) debug ──
        try:
            enabled_buttons = self.page.evaluate("""
                () => {
                    const results = [];
                    const selector = 'button, a.v-btn, [role="button"], .v-btn';
                    for (const el of document.querySelectorAll(selector)) {
                        const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true'
                                        || el.classList.contains('v-btn--disabled')
                                        || el.classList.contains('v-stepper-item');
                        if (disabled) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        results.push({
                            tag: el.tagName.toLowerCase(),
                            text: (el.innerText || el.textContent || '').trim().slice(0, 60),
                            aria: el.getAttribute('aria-label') || '',
                            classes: (el.className || '').slice(0, 100),
                            x: Math.round(rect.x), y: Math.round(rect.y),
                        });
                    }
                    return results;
                }
            """)
            logger.error("Enabled clickable elements on page: %d found", len(enabled_buttons))
            for b in enabled_buttons[:30]:
                logger.error("  %s text=%r aria=%r classes=%r pos=(%d,%d)",
                             b['tag'], b['text'], b['aria'], b['classes'], b['x'], b['y'])

            # Screenshot for visual debug
            shot_path = Path.home() / "Desktop" / "SiftStack" / "scratch" / "smartskip_recon" / \
                        f"wizard_stuck_{time.strftime('%H%M%S')}.png"
            shot_path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(shot_path), full_page=True)
            logger.error("Screenshot saved: %s", shot_path)
        except Exception as e:
            logger.error("Diagnostic dump failed: %s", e)

        raise SmartSkipMappingError(
            f"No enabled wizard action button matched any of {action_labels}. "
            f"See logger.error output above for enabled elements + screenshot. "
            f"Update action_labels in smartskip_client.py based on what you see."
        )

    # SmartSkip field label → our submission CSV column name (as it appears
    # on the draggable chip on the left panel, formatted "<Col Name>: <value>").
    # Property fields intentionally excluded — see SUBMISSION_COLUMNS docstring.
    _FIELD_TO_CHIP_LABEL: tuple[tuple[str, str], ...] = (
        # Required (must be mapped for Next to enable)
        ("FIRST NAME",       "First Name"),
        ("LAST NAME",        "Last Name"),
        ("MAILING ADDRESS",  "Mailing Address"),
        # Optional but improve match rate
        ("MIDDLE NAME",      "Middle Name"),
        ("MAILING CITY",     "Mailing City"),
        ("MAILING STATE",    "Mailing State"),
        ("MAILING ZIP",      "Mailing Zip"),
    )

    def _dump_mapping_dom(self, reason: str) -> None:
        """Diagnostic: on drag failure, save screenshot + dump the mapping DOM."""
        try:
            ts = time.strftime("%H%M%S")
            shot = Path.home() / "Desktop" / "SiftStack" / "scratch" / "smartskip_recon" / f"map_dom_{ts}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(shot), full_page=True)

            elements = self.page.evaluate("""
                () => {
                    const out = {chips: [], drop_targets: []};
                    // Chips: leaf-ish elements whose text starts with a
                    // known SmartSkip column prefix (First Name:, etc.)
                    const chipPrefixes = [
                        'First Name:', 'Last Name:', 'Middle Name:',
                        'Mailing Address:', 'Mailing City:', 'Mailing State:',
                        'Mailing Zip:', 'Property Address:', 'Property City:',
                        'Property State:', 'Property Zip:',
                    ];
                    for (const el of document.querySelectorAll('div, span, li')) {
                        const text = (el.innerText || '').trim();
                        if (!text || text.length > 60) continue;
                        for (const prefix of chipPrefixes) {
                            if (text.startsWith(prefix)) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width === 0 || rect.height === 0) continue;
                                // Only care about elements without lots of children
                                // (avoid matching wrapper containers)
                                if (el.querySelectorAll('*').length > 5) break;
                                out.chips.push({
                                    tag: el.tagName.toLowerCase(),
                                    text: text.slice(0, 60),
                                    cls: ((el.className && typeof el.className === 'string')
                                          ? el.className : '').slice(0, 80),
                                    draggable: el.draggable || el.getAttribute('draggable') === 'true',
                                    x: Math.round(rect.x), y: Math.round(rect.y),
                                    w: Math.round(rect.width),
                                });
                                break;
                            }
                        }
                    }
                    // Drop targets: divs/spans containing 'Required' text
                    // OR positioned to the right of a field label
                    const targetLabels = [
                        'FIRST NAME', 'LAST NAME', 'MIDDLE NAME',
                        'MAILING ADDRESS', 'MAILING CITY', 'MAILING STATE',
                        'MAILING ZIP', 'PROPERTY ADDRESS',
                    ];
                    for (const el of document.querySelectorAll('div, span')) {
                        const text = (el.innerText || '').trim();
                        if (text !== 'Required' && !text.startsWith('Required')) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0) continue;
                        out.drop_targets.push({
                            tag: el.tagName.toLowerCase(),
                            text: text.slice(0, 30),
                            cls: ((el.className && typeof el.className === 'string')
                                  ? el.className : '').slice(0, 80),
                            x: Math.round(rect.x), y: Math.round(rect.y),
                            w: Math.round(rect.width),
                        });
                    }
                    return out;
                }
            """)
            logger.error("=== DOM dump (%s) ===", reason)
            logger.error("Chips (by text pattern): %d", len(elements['chips']))
            for c in elements['chips'][:20]:
                logger.error("  %s text=%r cls=%r drag=%s pos=(%d,%d,w=%d)",
                             c['tag'], c['text'], c['cls'], c['draggable'],
                             c['x'], c['y'], c['w'])
            logger.error("Drop targets ('Required' boxes): %d", len(elements['drop_targets']))
            for t in elements['drop_targets'][:20]:
                logger.error("  %s text=%r cls=%r pos=(%d,%d,w=%d)",
                             t['tag'], t['text'], t['cls'], t['x'], t['y'], t['w'])
            logger.error("Screenshot: %s", shot)
        except Exception as e:
            logger.error("DOM dump failed: %s", e)

    # Field order matches SmartSkip's right-panel display order (verified
    # via DOM dump 2026-08-05). Used to match drop zones by index.
    _FIELD_ORDER: tuple[str, ...] = (
        "FIRST NAME", "LAST NAME", "MAILING ADDRESS", "MIDDLE NAME",
        "MAILING CITY", "MAILING STATE", "MAILING ZIP",
        "PROPERTY ADDRESS", "PROPERTY CITY", "PROPERTY STATE", "PROPERTY ZIP",
    )

    def _find_chip(self, chip_label: str):
        """Find a draggable chip by its label text.

        SmartSkip's DOM structure (verified 2026-08-05 DOM dump):
            <div class="chip-container">
              <div class="drag-handle">⋮⋮</div>
              <span class="item-name">First Name:</span>
              <span class="item-value">Franklin</span>
            </div>

        The parent chip-container has the JS drag handler. We locate the
        item-name span (which has our label) then walk up to its
        interactable parent.
        """
        # Locate the label span first — this is stable per DOM dump
        span = self.page.locator(
            f'span.item-name:text-is("{chip_label}:")'
        ).first
        if span.count() == 0:
            # Fallback: any span containing the exact "<label>:" text
            span = self.page.locator(
                f'span:text-is("{chip_label}:")'
            ).first
            if span.count() == 0:
                return None
        # Walk up to the chip container (draggable parent)
        # xpath=.. gets the immediate parent, xpath=../.. gets grandparent
        # SortableJS usually attaches to the direct list-item parent.
        return span.locator("xpath=..").first

    def _find_drop_slot(self, field_label: str):
        """Find the drop zone for a SmartSkip field label.

        DOM (verified 2026-08-05):
            <div class="final-zone [final-zone-error]">
              <span class="final-zone-placeholder">Required</span>
            </div>

        Drop zones render in DOM order matching _FIELD_ORDER. We select the
        nth .final-zone div for each field. This is more robust than
        matching by label text (labels are separate elements).
        """
        if field_label not in self._FIELD_ORDER:
            return None
        idx = self._FIELD_ORDER.index(field_label)
        # Use nth() on the .final-zone list — DOM order matches display order
        zone = self.page.locator(".final-zone").nth(idx)
        try:
            zone.wait_for(state="visible", timeout=2000)
            return zone
        except PWTimeout:
            return None

    def _manual_drag(self, chip, target) -> bool:
        """Perform a mouse-based drag from chip's DRAG HANDLE to target.

        SmartSkip's chips have a drag handle (⋮⋮ dot icon) at their LEFT
        edge — SortableJS-style drag libraries respond to mousedown ONLY
        when it hits the handle. Starting the drag at chip center often
        misses the handler.

        Uses (chip.x + ~14px, chip.center_y) as the start position to
        reliably hit the handle.
        """
        try:
            chip.scroll_into_view_if_needed(timeout=2000)
            self.page.wait_for_timeout(150)
            chip_box = chip.bounding_box()
            target_box = target.bounding_box()
            if not chip_box or not target_box:
                logger.debug("bounding_box returned None — element off-screen?")
                return False

            # Start at the drag handle position (LEFT ~14px of the chip)
            # rather than center — SortableJS-style libraries require the
            # mousedown to land on the handle element specifically.
            start_x = chip_box["x"] + min(14, chip_box["width"] * 0.15)
            start_y = chip_box["y"] + chip_box["height"] / 2
            end_x = target_box["x"] + target_box["width"] / 2
            end_y = target_box["y"] + target_box["height"] / 2

            self.page.mouse.move(start_x, start_y)
            self.page.wait_for_timeout(100)
            self.page.mouse.down()
            self.page.wait_for_timeout(200)   # let mousedown settle
            # Very gradual: 20 steps + long inter-step waits helps
            # SortableJS's throttled dragover handlers fire cleanly.
            for step in range(1, 21):
                self.page.mouse.move(
                    start_x + (end_x - start_x) * step / 20,
                    start_y + (end_y - start_y) * step / 20,
                    steps=3,
                )
                self.page.wait_for_timeout(50)
            # Hover briefly on the target so the drop indicator activates
            self.page.wait_for_timeout(300)
            self.page.mouse.up()
            self.page.wait_for_timeout(300)   # let drop event process
            return True
        except Exception as e:
            logger.debug("Manual drag failed: %s", e)
            return False

    def _drag_chip_to_slot(self, chip_label: str, field_label: str) -> bool:
        """Find + drag a column chip to its matching slot.

        Uses manual mouse events (not HTML5 drag_to) — SmartSkip's drag
        library ignores the latter (verified 2026-08-05 via DOM inspection).
        """
        chip = self._find_chip(chip_label)
        if chip is None:
            logger.debug("Chip not found for %r", chip_label)
            return False
        target = self._find_drop_slot(field_label)
        if target is None:
            logger.debug("Drop slot not found for %r", field_label)
            return False
        return self._manual_drag(chip, target)

    def _wizard_step_2_map_columns(self) -> None:
        """Step 2: Drag each column chip to its matching slot.

        SmartSkip does NOT auto-map through programmatic upload (verified
        2026-08-05 — auto-map fires only on manual-user upload path).
        We explicitly drag every chip we have data for, then click Next.
        """
        logger.info("SmartSkip wizard step 2: drag chips to slots")
        # Give the page time to fully render draggable chips
        self.page.wait_for_timeout(2500)

        mapped = 0
        failed_required = []
        for field_label, chip_label in self._FIELD_TO_CHIP_LABEL:
            if self._drag_chip_to_slot(chip_label, field_label):
                mapped += 1
            elif field_label in ("FIRST NAME", "LAST NAME", "MAILING ADDRESS"):
                failed_required.append(field_label)

        logger.info("Mapped %d/%d columns", mapped, len(self._FIELD_TO_CHIP_LABEL))

        if failed_required:
            # Dump DOM state so we can see what selectors we should be using
            self._dump_mapping_dom(f"failed_required={failed_required}")
            raise SmartSkipMappingError(
                f"Failed to map required fields via drag: {failed_required}. "
                f"See DOM dump above and screenshot at scratch/smartskip_recon/map_dom_*.png"
            )

        # After mapping, wait a beat for Vue to re-render + Next to enable
        self.page.wait_for_timeout(1500)

        self._click_wizard_action_button(("Next", "Continue", "Preview"))

        # Wait for Preview step marker. `.first` disambiguates because
        # the word "Preview" appears in both the stepper header and the
        # preview panel heading — strict mode would error without it.
        self.page.get_by_text("Preview", exact=False).first.wait_for(
            state="visible", timeout=STEP_ADVANCE_S * 1000
        )

    def _wizard_step_3_preview(self) -> None:
        """Step 3: Verify preview table renders + click Next."""
        logger.info("SmartSkip wizard step 3: preview + advance")
        self._click_wizard_action_button(("Next", "Continue", "Confirm"))

        # "Payment" text appears 3× on the payment step (stepper title,
        # section heading, "Total payment" label) — .first picks any one
        # of them; presence of any is sufficient to confirm advance.
        self.page.get_by_text("Payment", exact=False).first.wait_for(
            state="visible", timeout=STEP_ADVANCE_S * 1000
        )

    def _wizard_step_4_payment(self) -> None:
        """Step 4: Pay via CC on file. Handles the confirmation modal.

        SmartSkip flow (verified 2026-08-06 via operator report):
          1. Wizard's Payment step shows cost breakdown + "Pay" button
          2. Clicking "Pay" opens a Stripe/payment confirmation modal
          3. Modal has a distinct "Confirm" / "Pay Now" / "Proceed" button
          4. Only after modal-confirm does the CC charge process
          5. Wizard advances to Step 5 Finish

        Prior bug: JS-click fallback grabbed the wizard's "Pay" button but
        then found the modal's Cancel/Close button on the second pass,
        dismissing the modal without payment. Result: no charge, wizard
        reset to landing page.

        Fix: after clicking Pay, screenshot the modal state + specifically
        target modal-confirm buttons before advancing to Step 5 check.
        """
        logger.info("SmartSkip wizard step 4: payment confirm")
        self.page.wait_for_timeout(1500)

        # Look for an insufficient-balance warning before clicking
        if self.page.get_by_text("insufficient", exact=False).count():
            raise SmartSkipPaymentError(
                "SmartSkip reports insufficient account balance / no CC "
                "on file — top up + add card at app.smartskip.io/billing."
            )

        # Click the initial Pay button on the wizard's payment panel
        self._click_wizard_action_button(
            ("Pay", "Confirm", "Submit", "Continue", "Next")
        )

        # Diagnostic screenshot IMMEDIATELY after Pay click — helps me see
        # any modal that appears
        try:
            ts = time.strftime("%H%M%S")
            shot = (Path.home() / "Desktop" / "SiftStack" / "scratch"
                    / "smartskip_recon" / f"post_pay_click_{ts}.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            self.page.wait_for_timeout(1500)
            self.page.screenshot(path=str(shot), full_page=True)
            logger.info("  Post-Pay-click screenshot saved: %s", shot)
        except Exception:
            pass

        # Look for the modal's "Pay $ X.XX" button. The dollar-sign pattern is
        # UNIQUE — the wizard's own button says "Pay and get results" (no $).
        # We poll for up to 10 seconds; the modal has render animation.
        # Verified 2026-08-06 via screenshot: modal shows "Pay $ 0.75" button.
        pay_button_deadline = time.time() + 10
        pay_button_clicked = False
        while time.time() < pay_button_deadline:
            js_result = self.page.evaluate("""
                () => {
                    // Find any enabled button whose visible text starts with "Pay $"
                    // — this is UNIQUE to the modal's confirm button.
                    for (const btn of document.querySelectorAll('button')) {
                        if (btn.disabled) continue;
                        const rect = btn.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const text = (btn.innerText || btn.textContent || '').trim();
                        if (text.startsWith('Pay $') || text.startsWith('Pay$')) {
                            btn.scrollIntoView({block: 'center'});
                            btn.click();
                            return {clicked: true, text};
                        }
                    }
                    return {clicked: false};
                }
            """)
            if js_result.get("clicked"):
                logger.info("  Modal confirmed: clicked %r", js_result["text"])
                pay_button_clicked = True
                break
            self.page.wait_for_timeout(500)

        if not pay_button_clicked:
            logger.info("  No 'Pay $ X.XX' button found — payment may have gone through directly")

        # Wait for Finish step. Use specific finish text (not landing-page-shared).
        # `.first` avoids strict mode ambiguity.
        try:
            self.page.get_by_text("being processed", exact=False).first.wait_for(
                state="visible", timeout=STEP_ADVANCE_S * 1000
            )
        except PWTimeout:
            # If "being processed" doesn't show, dump DOM so we see current state
            self._dump_mapping_dom("step_4_no_finish_after_pay")
            raise SmartSkipMappingError(
                "After clicking Pay + attempting modal-confirm, could not "
                "detect Finish step within 15s. Either the payment modal "
                "requires a different button, OR the payment flow uses a "
                "Stripe redirect (new page/tab). See DOM dump + screenshot."
            )

    def _wizard_step_5_finish(self) -> None:
        """Step 5: Confirm the finish state, return to Bulk Skip.

        SmartSkip's finish screen may show any of several messages:
        "Your file is being processed", "Successfully uploaded",
        "Processing your request", "Thank you", or may auto-redirect back
        to Bulk Skip. We try each pattern; if none match within a short
        window, we still navigate to Bulk Skip and consider the submit
        successful (payment went through in Step 4, batch is in flight).
        """
        logger.info("SmartSkip wizard step 5: finish")
        # IMPORTANT: these must be text patterns UNIQUE to the Finish step.
        # Do NOT include patterns that also appear on the Bulk Skip landing
        # page header (e.g. "email you", "$0.50 minimum") — those match false
        # positives if the wizard resets on payment failure.
        finish_indicators = [
            "being processed",              # "Your file is being processed"
            "close this page",              # "You can close this page..."
            "not necessary to wait",        # unique to finish
            "may take some time",           # unique to finish
            "Back to Home",                 # button label on finish
            "successfully uploaded",        # possible variant
            "processing your request",      # possible variant
        ]
        confirmed = False
        for text in finish_indicators:
            try:
                self.page.get_by_text(text, exact=False).first.wait_for(
                    state="visible", timeout=3000
                )
                logger.info("  Finish confirmed via text: %r", text)
                confirmed = True
                break
            except PWTimeout:
                continue

        if not confirmed:
            # Could NOT confirm Finish step — this now means one of:
            #   1. Payment silently failed and wizard reset to landing page
            #   2. SmartSkip UI changed and none of our finish patterns match
            #   3. Payment succeeded but finish page loaded slowly
            # Since we can't distinguish, dump DOM + raise so operator can
            # verify state before assuming the batch is queued.
            self._dump_mapping_dom("step_5_finish_not_confirmed")
            raise SmartSkipMappingError(
                "Could not detect Finish confirmation on Step 5 within 20s. "
                "Payment may have silently failed at Step 4 (wizard reset to "
                "landing page) OR the Finish UI changed. See DOM dump + "
                "screenshot to determine actual state. Check Skips History "
                "manually to see if a batch was actually submitted."
            )

        # Navigate back to Bulk Skip so Skips History table is available
        # for the downloader when it polls.
        self.page.goto(BULK_SKIP_URL, wait_until="domcontentloaded")

    # ── Poll + Download ───────────────────────────────────────────────

    def wait_and_download(
        self,
        batch: SmartSkipBatch,
        *,
        timeout_s: int = PROCESSING_MAX_S,
    ) -> Path:
        """Poll Skips History for batch.batch_id (matches on filename column)
        until Status == Completed, then click Download.

        Returns local path to the downloaded result CSV.
        Raises SmartSkipTimeoutError if not Completed within timeout_s.
        """
        self._assert_logged_in()
        deadline = time.time() + timeout_s
        last_status = None
        while time.time() < deadline:
            self.page.goto(BULK_SKIP_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1500)

            status = self._skips_history_status(batch.batch_id)
            if status != last_status:
                logger.info("SmartSkip batch %s status: %s", batch.batch_id, status)
                last_status = status

            if status == "Completed":
                return self._download_result(batch)
            if status == "Failed":
                raise SmartSkipBatchFailed(
                    f"SmartSkip reports Failed status for {batch.batch_id}"
                )
            time.sleep(PROCESSING_POLL_S)

        raise SmartSkipTimeoutError(
            f"SmartSkip batch {batch.batch_id} did not complete in {timeout_s}s"
        )

    def _skips_history_status(self, batch_id: str) -> str:
        """Return the Status cell for a specific batch row in Skips History."""
        # The Skips History table is on the Bulk Skip landing page.
        # Find the row containing the filename, then read the Status cell.
        row = self.page.locator("tr, [role=row]").filter(has_text=batch_id).first
        if row.count() == 0:
            return "NotFound"
        # Status cell has text like "● Completed" / "Processing" / "Failed"
        text = row.inner_text().strip()
        for keyword in ("Completed", "Processing", "Failed", "Pending"):
            if keyword.lower() in text.lower():
                return keyword
        return "Unknown"

    def _download_result(self, batch: SmartSkipBatch) -> Path:
        """Click the Download icon in the batch's row + save the file.

        SmartSkip's download element is an icon-button in the Download column
        of the Skips History row. Playwright's expect_download() sometimes
        times out because the download may fire via JS with a blob: URL that
        the event listener doesn't catch. We register the download listener
        BEFORE the click via context.on() so we don't miss the event, and
        use JS-based click (bypasses potential pointer intercepts).
        """
        out_path = self.download_dir / "results" / f"result_{batch.batch_id}"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Attach an event listener BEFORE the click so we don't miss the event
        download_future: list = []
        def _capture_download(d):
            download_future.append(d)
        self.page.on("download", _capture_download)

        # Use JS to find the download element in the row and click it.
        # The row contains: File name link, dates, status, results count, then
        # the download icon. Icon is typically the LAST clickable element in
        # the row (button OR anchor).
        clicked = self.page.evaluate("""
            (batchId) => {
                // Find every row that contains the batch filename
                for (const row of document.querySelectorAll('tr, [role="row"], .row')) {
                    const text = (row.innerText || '').trim();
                    if (!text.includes(batchId)) continue;
                    // Find the download element — try button first, then anchor
                    const buttons = row.querySelectorAll('button, a');
                    if (buttons.length === 0) return {clicked: false, reason: 'no buttons in row'};
                    // Last one is typically the download
                    const btn = buttons[buttons.length - 1];
                    btn.scrollIntoView({block: 'center'});
                    btn.click();
                    return {clicked: true, tag: btn.tagName.toLowerCase(),
                            text: (btn.innerText || '').trim().slice(0, 40),
                            href: btn.getAttribute('href') || ''};
                }
                return {clicked: false, reason: 'no row matched batch id'};
            }
        """, batch.batch_id)

        if not clicked.get("clicked"):
            self.page.off("download", _capture_download)
            raise SmartSkipTimeoutError(
                f"Could not find download element for batch {batch.batch_id}: "
                f"{clicked.get('reason', 'unknown')}"
            )
        logger.info("  Download-click via JS: %s text=%r href=%r",
                    clicked.get("tag"), clicked.get("text"), clicked.get("href"))

        # If the element was an anchor with an href, fetch the file via HTTP
        # (using the browser's cookies — same session as the click).
        if clicked.get("tag") == "a" and clicked.get("href"):
            href = clicked["href"]
            # Make absolute URL if href is relative
            if href.startswith("/"):
                href = f"{SMARTSKIP_BASE_URL}{href}"
            logger.info("  Fetching CSV via HTTP: %s", href)
            resp = self.page.request.get(href)
            if resp.ok:
                out_path.write_bytes(resp.body())
                logger.info("  Downloaded via HTTP: %s (%d bytes)",
                            out_path, len(resp.body()))
                self.page.off("download", _capture_download)
                return out_path

        # Otherwise wait for a download event to fire (up to 60s)
        deadline = time.time() + 60
        while time.time() < deadline and not download_future:
            self.page.wait_for_timeout(500)

        self.page.off("download", _capture_download)

        if not download_future:
            raise SmartSkipTimeoutError(
                f"Download click for {batch.batch_id} did not trigger a "
                f"download event in 60s. Result is available in Skips History "
                f"— manually download + use --merge-from-csv to process."
            )

        download = download_future[0]
        download.save_as(str(out_path))
        logger.info("  Downloaded via download event: %s", out_path)
        return out_path


# ─────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────


class SmartSkipError(Exception):
    """Base class for SmartSkip client failures."""


class SmartSkipSessionExpired(SmartSkipError):
    """Persistent-profile session has expired — operator must re-login."""


class SmartSkipMappingError(SmartSkipError):
    """Column mapping step failed — wizard UI may have changed."""


class SmartSkipPaymentError(SmartSkipError):
    """Payment step failed — likely insufficient account balance."""


class SmartSkipTimeoutError(SmartSkipError):
    """Batch did not complete within the polling timeout."""


class SmartSkipBatchFailed(SmartSkipError):
    """SmartSkip reports Failed status on the batch."""


__all__ = [
    "SmartSkipRow", "SmartSkipContact", "SmartSkipResult", "SmartSkipBatch",
    "SmartSkipClient",
    "write_submission_csv", "parse_result_csv", "format_note",
    "SUBMISSION_COLUMNS", "REQUIRED_COLUMNS",
    "SmartSkipError", "SmartSkipSessionExpired", "SmartSkipMappingError",
    "SmartSkipPaymentError", "SmartSkipTimeoutError", "SmartSkipBatchFailed",
]
