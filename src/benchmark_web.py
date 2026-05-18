"""Benchmark Web adapter for Jefferson County, AL Probate Court case data.

Source: https://benchmarkweb.jccal.org — Pioneer Technology Group's
Benchmark Web case management system, used by Jefferson County's Estate
Division for wills/probate filings. Login-required (credentials in
.env: BENCHMARK_EMAIL, BENCHMARK_PASSWORD).

This is the system that vendors like RealSupermarket use as their
upstream source for AL probate court case data. Returns case number,
file date, judge, court type, and ALL parties (decedent, petitioner,
attorney, next-of-kin, etc.) with party type labels — but NO addresses.
Address resolution happens downstream via property API + obituary
cross-reference.

Example usage from CLI::

    python src/benchmark_web.py --days-back 7

Programmatic::

    async with BenchmarkSession() as bw:
        cases = await bw.list_cases_in_date_range(
            start=date(2026, 4, 27),
            end=date(2026, 5, 4),
        )
        for c in cases:
            print(c.case_number, c.decedent_name, c.petitioner_name)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Iterator

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright

logger = logging.getLogger(__name__)

# ── URLs ──────────────────────────────────────────────────────────────
BASE_URL = "https://benchmarkweb.jccal.org"
LOGIN_URL = f"{BASE_URL}/web/home.aspx/search"
CASE_DETAIL_PATH = "/Web/CourtCase.aspx/Details"

# ── Selectors ─────────────────────────────────────────────────────────
SEL_USERNAME = "input#txtUsername"
SEL_PASSWORD = "input#txtPassword"
SEL_LOGIN_BTN = "a#btnLogin"
SEL_OPENED_FROM = "input#openedFrom"
SEL_OPENED_TO = "input#openedTo"
SEL_SEARCH_BTN = "button#searchButton"
SEL_LOGOUT = "a#btnLogout"

# Accordion expand triggers on case detail page
SEL_SUMMARY_ACCORDION = 'a[href="#summaryAccordionCollapse"]'
SEL_PARTIES_ACCORDION = 'a[href="#partyAccordionCollapse"]'
SEL_DOCKETS_ACCORDION = 'a[href="#caseDocketsAccordionCollapse"]'

# Accordion content containers
ID_SUMMARY = "summaryAccordionCollapse"
ID_PARTIES = "partyAccordionCollapse"
ID_DOCKETS = "caseDocketsAccordionCollapse"


# ── Data classes ──────────────────────────────────────────────────────


@dataclass
class BenchmarkParty:
    """A single party on a probate case."""
    party_type: str           # PETITIONER, DECEDENT, ATTORNEY, JUDGE, NEXT OF KIN, ADMINISTRATOR, etc.
    name: str                 # "STREUFERT, TERA BROOKE" (LAST, FIRST MIDDLE format)
    is_alias: bool = False    # True when name was marked "(Alias)" — alternate spelling/form


@dataclass
class BenchmarkCase:
    """Structured probate case record from Benchmark Web."""
    case_number: str          # "26BHM000826" — year+division+sequence
    case_url: str             # full URL to case detail
    file_date: str            # YYYY-MM-DD (parsed from "Clerk File Date")
    case_type: str            # "WILL", "ESTATE", etc.
    court_type: str           # "BIRMINGHAM" or "BESSEMER" division
    status: str               # "OPEN" / "CLOSED"
    judge: str                # "BLANCHARD, YASHIBA"
    parties: list[BenchmarkParty] = field(default_factory=list)
    docket_entries: list[str] = field(default_factory=list)  # raw docket text lines

    # Convenience accessors
    @property
    def decedent_name(self) -> str:
        for p in self.parties:
            if p.party_type == "DECEDENT" and not p.is_alias:
                return p.name
        return ""

    @property
    def petitioner_name(self) -> str:
        # Prefer PETITIONER; fall back to PERSONAL REPRESENTATIVE / ADMINISTRATOR
        priority = ["PETITIONER", "PERSONAL REPRESENTATIVE", "ADMINISTRATOR"]
        for ptype in priority:
            for p in self.parties:
                if p.party_type == ptype and not p.is_alias:
                    return p.name
        return ""

    @property
    def attorney_name(self) -> str:
        for p in self.parties:
            if p.party_type == "ATTORNEY" and not p.is_alias:
                return p.name
        return ""

    @property
    def next_of_kin(self) -> list[str]:
        return [p.name for p in self.parties if p.party_type == "NEXT OF KIN" and not p.is_alias]


# ── Session manager ───────────────────────────────────────────────────


class BenchmarkSession:
    """Async context manager that handles login + browser lifecycle.

    Usage::

        async with BenchmarkSession() as bw:
            cases = await bw.list_cases_in_date_range(start, end)
    """

    def __init__(self, headless: bool = True):
        load_dotenv()
        self.email = os.getenv("BENCHMARK_EMAIL", "").strip()
        self.password = os.getenv("BENCHMARK_PASSWORD", "").strip()
        if not self.email or not self.password:
            raise RuntimeError(
                "BENCHMARK_EMAIL and BENCHMARK_PASSWORD must be set in .env"
            )
        self.headless = headless
        self._pw: Playwright | None = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "BenchmarkSession":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        await self._login()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _login(self) -> None:
        page = self._page
        assert page is not None
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)
        await page.fill(SEL_USERNAME, self.email)
        await page.fill(SEL_PASSWORD, self.password)
        await page.click(SEL_LOGIN_BTN)
        # Wait for either the post-login UI or an error indicator
        await page.wait_for_timeout(4000)
        # Verify by checking for the Logout link
        logout = await page.query_selector(SEL_LOGOUT)
        if not logout:
            # Fallback: check if URL still contains Search but body has Logout text
            body_text = await page.evaluate("() => document.body.textContent.toLowerCase()")
            if "logout" not in body_text or "registered user" not in body_text:
                raise RuntimeError("Benchmark Web login appears to have failed")
        logger.info("Benchmark Web login successful (user: %s)", self.email)

    # ── Search ────────────────────────────────────────────────────────

    async def list_cases_in_date_range(
        self, start: date, end: date,
    ) -> list[BenchmarkCase]:
        """Search by Clerk-Filed-Date range and return case records.

        Args:
            start: inclusive start date (uses MM/DD/YYYY for the form)
            end: inclusive end date

        Returns: list of BenchmarkCase, ordered as the site returned them
            (typically newest first).
        """
        page = self._page
        assert page is not None

        # Navigate to search page (re-navigation resets the form cleanly)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)
        await page.fill(SEL_OPENED_FROM, start.strftime("%m/%d/%Y"))
        await page.fill(SEL_OPENED_TO, end.strftime("%m/%d/%Y"))
        await page.click(SEL_SEARCH_BTN)
        await page.wait_for_timeout(5000)

        if "/Web/CourtCase.aspx/CaseSearch" not in page.url:
            logger.warning("Unexpected URL after search: %s", page.url)

        # Collect (case_number, case_url) pairs from the result table
        case_links = await page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="Details"]');
            const seen = new Map();
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const txt = a.textContent.trim();
                const title = a.getAttribute('title') || '';
                // Pull case number from text or title ("View Case Details for 26BHM000826")
                let caseNum = '';
                if (/^[0-9]{2}[A-Z]{3}[0-9]+$/.test(txt)) {
                    caseNum = txt;
                } else {
                    const m = title.match(/([0-9]{2}[A-Z]{3}[0-9]+)/);
                    if (m) caseNum = m[1];
                }
                if (caseNum && href.includes('Details') && !seen.has(caseNum)) {
                    seen.set(caseNum, href);
                }
            }
            return Array.from(seen.entries());
        }""")
        logger.info("Found %d unique cases in range %s..%s",
                    len(case_links), start, end)

        cases: list[BenchmarkCase] = []
        for case_num, href in case_links:
            full_url = BASE_URL + href if href.startswith("/") else href
            try:
                bc = await self.fetch_case_detail(full_url, case_num)
                cases.append(bc)
            except Exception as e:
                logger.warning("Failed to fetch case %s (%s): %s", case_num, full_url[:80], e)
        return cases

    # ── Case detail ───────────────────────────────────────────────────

    async def fetch_case_detail(
        self, case_url: str, case_number_hint: str = "",
    ) -> BenchmarkCase:
        """Navigate to a case detail page and parse all sections."""
        page = self._page
        assert page is not None

        await page.goto(case_url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        # Expand each accordion individually with a delay (clicking all at once
        # has been observed to redirect to InvalidCase)
        for sel in (SEL_SUMMARY_ACCORDION, SEL_PARTIES_ACCORDION, SEL_DOCKETS_ACCORDION):
            try:
                await page.click(sel, timeout=4_000)
                await page.wait_for_timeout(1500)
            except Exception as e:
                logger.debug("Accordion click skipped (%s): %s", sel, e)

        summary = await self._parse_summary()
        parties = await self._parse_parties()
        dockets = await self._parse_dockets()

        return BenchmarkCase(
            case_number=summary.get("case_number") or case_number_hint,
            case_url=case_url,
            file_date=summary.get("file_date", ""),
            case_type=summary.get("case_type", ""),
            court_type=summary.get("court_type", ""),
            status=summary.get("status", ""),
            judge=summary.get("judge", ""),
            parties=parties,
            docket_entries=dockets,
        )

    async def _parse_summary(self) -> dict:
        """Extract key/value pairs from the Summary accordion."""
        page = self._page
        assert page is not None
        text = await page.evaluate(f"""() => {{
            const el = document.getElementById('{ID_SUMMARY}');
            return el ? el.textContent : '';
        }}""")
        text = re.sub(r"\s+", " ", text).strip()

        def grab(label: str, pattern_extra: str = r"[\w\s.,'\-/]+?") -> str:
            m = re.search(rf"{label}\s*:?\s*({pattern_extra})\s+(?:\b[A-Z][a-z]+\b|$|(?:Total|Court|Status|Case|Custody|Waive|Agency))",
                          text, re.IGNORECASE)
            return m.group(1).strip() if m else ""

        # Targeted extracts
        case_number = ""
        m = re.search(r"Case\s+Number\s*:?\s*([0-9]{2}[A-Z]{3}\d+)", text)
        if m:
            case_number = m.group(1)
        file_date = ""
        m = re.search(r"Clerk\s+File\s+Date\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", text)
        if m:
            file_date = _to_iso_date(m.group(1))
        case_type = ""
        m = re.search(r"Case\s+Type\s*:?\s*([A-Za-z][\w\s.,]*?)\s+Status\s*:", text)
        if m:
            case_type = m.group(1).strip(". ")
        court_type = ""
        m = re.search(r"Court\s+Type\s*:?\s*([A-Z]+)\s+Uniform", text)
        if m:
            court_type = m.group(1).strip()
        status = ""
        m = re.search(r"\bStatus\s*:?\s*(OPEN|CLOSED|PENDING|DISMISSED)\b", text)
        if m:
            status = m.group(1)
        judge = ""
        m = re.search(r"Judge\s*:?\s*([A-Z][A-Z\s,.'\-]+?)\s+Case\s+Number", text)
        if m:
            judge = m.group(1).strip(", ")
        return {
            "case_number": case_number,
            "file_date": file_date,
            "case_type": case_type,
            "court_type": court_type,
            "status": status,
            "judge": judge,
        }

    async def _parse_parties(self) -> list[BenchmarkParty]:
        """Extract structured parties from the Parties accordion."""
        page = self._page
        assert page is not None
        rows = await page.evaluate(f"""() => {{
            const sec = document.getElementById('{ID_PARTIES}');
            if (!sec) return [];
            // Find the parties table inside
            const trs = sec.querySelectorAll('table tr');
            const out = [];
            for (const tr of trs) {{
                const tds = tr.querySelectorAll('td');
                if (tds.length >= 2) {{
                    out.push({{
                        type: (tds[0].textContent || '').trim(),
                        name: (tds[1].textContent || '').trim(),
                    }});
                }}
            }}
            return out;
        }}""")
        parties: list[BenchmarkParty] = []
        for r in rows:
            ptype = r.get("type", "").upper().strip()
            name = r.get("name", "").strip()
            if not ptype or not name:
                continue
            # Skip header rows
            if ptype in {"TYPE", "PARTY NAME", "PARTY"}:
                continue
            is_alias = "(ALIAS)" in name.upper()
            if is_alias:
                name = re.sub(r"\s*\(Alias\)\s*$", "", name, flags=re.IGNORECASE).strip()
            parties.append(BenchmarkParty(party_type=ptype, name=name, is_alias=is_alias))
        return parties

    async def _parse_dockets(self) -> list[str]:
        """Extract docket entries (text only — Benchmark doesn't expose docs at our tier)."""
        page = self._page
        assert page is not None
        text = await page.evaluate(f"""() => {{
            const sec = document.getElementById('{ID_DOCKETS}');
            return sec ? sec.textContent : '';
        }}""")
        text = re.sub(r"\xa0", " ", text)
        # Lines look like: "9   4/27/2026 CERTIFICATE TO THE PROBATE OF WILL"
        entries = []
        for m in re.finditer(
            r"\b(\d{1,3})\s+(\d{1,2}/\d{1,2}/\d{4})\s+([A-Z][^0-9]{5,200}?)(?=\s+\d{1,3}\s+\d{1,2}/\d{1,2}/\d{4}|$)",
            text,
        ):
            seq, dt, desc = m.group(1), m.group(2), m.group(3).strip()
            entries.append(f"#{seq} {dt} {desc}")
        return entries


# ── Helpers ───────────────────────────────────────────────────────────


def _to_iso_date(mdy: str) -> str:
    """Convert MM/DD/YYYY → YYYY-MM-DD (or return original on failure)."""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        return mdy


# ── CLI ───────────────────────────────────────────────────────────────


async def _cli_main(args: argparse.Namespace) -> int:
    end = date.today()
    start = end - timedelta(days=args.days_back)

    async with BenchmarkSession(headless=not args.headed) as bw:
        cases = await bw.list_cases_in_date_range(start, end)

    if args.json:
        # Convert to JSON-friendly dict
        out = []
        for c in cases:
            d = asdict(c)
            out.append(d)
        print(json.dumps(out, indent=2))
    else:
        print(f"\n{len(cases)} case(s) filed {start} .. {end}\n")
        for c in cases[:args.limit]:
            print(f"  {c.case_number}  filed={c.file_date}  type={c.case_type}  status={c.status}")
            print(f"    decedent:   {c.decedent_name!r}")
            print(f"    petitioner: {c.petitioner_name!r}")
            print(f"    attorney:   {c.attorney_name!r}")
            print(f"    judge:      {c.judge!r}")
            print(f"    next-of-kin: {c.next_of_kin}")
            print(f"    docket entries: {len(c.docket_entries)}")
            print()
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pull Jefferson County probate cases from Benchmark Web.",
    )
    p.add_argument("--days-back", type=int, default=7,
                   help="How many days back from today to query (default 7)")
    p.add_argument("--limit", type=int, default=20,
                   help="Maximum cases to print to stdout (default 20)")
    p.add_argument("--json", action="store_true",
                   help="Output as JSON instead of human-readable")
    p.add_argument("--headed", action="store_true",
                   help="Run Chromium with a visible window (default headless)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(_cli_main(args))


if __name__ == "__main__":
    sys.exit(main())
