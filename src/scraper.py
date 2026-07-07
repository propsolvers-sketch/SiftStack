"""Core scraping logic — submit search form, paginate results, solve CAPTCHA."""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright

from captcha_solver import solve_captcha_and_view
import config

if TYPE_CHECKING:
    from observability import ServiceRateTracker
from config import (
    BASE_URL,
    CAPTCHA_FAILED_IDS_FILE,
    CAPTCHA_FAILED_PRUNE_DAYS,
    MAX_RETRIES,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    RESULTS_PER_PAGE,
    SAVED_SEARCHES,
    SEARCH_URL,
    SEEN_IDS_FILE,
    SEEN_IDS_PRUNE_DAYS,
    STATE_FILE,
    SearchConfig,
    SEL_NEXT_PAGE_BUTTON,
    SEL_PER_PAGE_DROPDOWN,
    SEL_RESULTS_GRID,
    SEL_SEARCH_DAYS,
    SEL_SEARCH_EXCLUDE,
    SEL_SEARCH_SUBMIT,
    SEL_SEARCH_TEXT,
    SEL_SEARCH_TYPE_AND,
    SEL_SEARCH_TYPE_OR,
)
from foreclosure_filter import is_valid_foreclosure
from notice_parser import (
    NoticeData,
    assign_target_county_from_text,
    is_target_county_async,
    parse_notice_page,
    snippet_passes_county_filter,
)

logger = logging.getLogger(__name__)


async def delay() -> None:
    """Random delay between requests to avoid detection."""
    wait = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(wait)


# ── Search Form ───────────────────────────────────────────────────────


def _get_session_base(page_url: str) -> str:
    """Extract the session-aware base URL from the current page URL."""
    m = re.search(r"(https?://[^/]+/(?:\(S\([^)]+\)\)/)?)", page_url)
    if m:
        return m.group(1)
    return BASE_URL + "/"


async def _submit_search(page: Page, search: SearchConfig, days_back: int) -> bool:
    """Navigate to Search.aspx, fill the form, and submit.

    Returns True if results grid appeared, False on failure.
    """
    logger.info("Submitting search: %s [%s] last %d days", search.search_terms, search.search_type, days_back)
    try:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        logger.error("Could not load Search.aspx: %s", exc)
        return False

    # Fill all fields via JS to avoid visibility/interception issues
    type_id = SEL_SEARCH_TYPE_OR[1:] if search.search_type == "OR" else SEL_SEARCH_TYPE_AND[1:]
    escaped = search.search_terms.replace("'", "\\'")
    escaped_ex = search.exclude_terms.replace("'", "\\'")
    await page.evaluate(f"""() => {{
        document.getElementById('{SEL_SEARCH_TEXT[1:]}').value = '{escaped}';
        document.getElementById('{type_id}').checked = true;
        document.getElementById('{SEL_SEARCH_EXCLUDE[1:]}').value = '{escaped_ex}';
        document.getElementById('{SEL_SEARCH_DAYS[1:]}').value = '{days_back}';
    }}""")

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
            await page.click(SEL_SEARCH_SUBMIT, force=True)
    except PwTimeout:
        logger.warning("Search submission navigation timed out — results may be in UpdatePanel")
    await page.wait_for_timeout(2000)

    # Verify results grid appeared (no grid = zero results, not an error)
    grid = await page.query_selector(SEL_RESULTS_GRID)
    if not grid:
        logger.info("No results found for search: %s", search.search_terms)
        return False

    return True


async def _set_per_page(page: Page) -> None:
    """Set results-per-page to 50 if dropdown is present."""
    dropdown = await page.query_selector(SEL_PER_PAGE_DROPDOWN)
    if dropdown:
        current = await dropdown.input_value()
        if current != str(RESULTS_PER_PAGE):
            await page.evaluate(
                f"document.querySelector('select[name$=\"ddlPerPage\"]').value = '{RESULTS_PER_PAGE}'"
            )
            try:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                    await page.evaluate(
                        "javascript:setTimeout('__doPostBack(\\'ctl00$ContentPlaceHolder1$WSExtendedGridNP1$GridView1$ctl01$ddlPerPage\\',\\'\\')', 0)"
                    )
            except PwTimeout:
                pass
            await page.wait_for_timeout(1500)


async def _get_page_info(page: Page) -> tuple[int, int]:
    """Parse 'X of Y Pages' from the results header. Returns (current, total)."""
    try:
        span = await page.query_selector("span[id$='lblCurrentPage']")
        total_span = await page.query_selector("span[id$='lblTotalPages']")
        if span and total_span:
            cur = int((await span.inner_text()).strip())
            total_text = (await total_span.inner_text()).strip()  # " of 9 Pages "
            m = re.search(r"of\s+(\d+)", total_text)
            total = int(m.group(1)) if m else cur
            return cur, total
    except Exception:
        pass
    return 1, 1


async def _click_next_page(page: Page, current_page: int) -> bool:
    """Click the AL site's "Next page" image-button and wait for the postback.

    AL's pagination is an ASP.NET image button that fires __doPostBack — the
    URL doesn't change. ``page.expect_navigation`` therefore times out forever
    even though the grid does update. Instead we click and poll the
    ``lblCurrentPage`` span until it shows a value different from
    ``current_page``.

    Returns True if the page advanced, False on click error or timeout.
    """
    next_btn = await page.query_selector(SEL_NEXT_PAGE_BUTTON)
    if not next_btn:
        return False
    disabled = await next_btn.get_attribute("disabled")
    if disabled:
        return False
    try:
        await next_btn.click(timeout=5_000)
        await page.wait_for_function(
            "(prev) => { "
            "const s = document.querySelector('span[id$=\"lblCurrentPage\"]'); "
            "return s && s.innerText.trim() !== prev; "
            "}",
            arg=str(current_page),
            timeout=15_000,
        )
        return True
    except Exception as e:
        logger.debug("Next-page click failed: %s", e)
        return False


async def _collect_notice_ids_on_page(page: Page) -> list[str]:
    """Collect all numeric notice IDs from hdnPKValue hidden inputs on current page."""
    return await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('input[id*="hdnPKValue"]'))
            .map(el => el.value)
            .filter(v => v && /^\\d+$/.test(v));
    }""")


async def _extract_row_data(page: Page) -> list[dict]:
    """Extract notice metadata from result rows (ID, publication date, snippet)."""
    return await page.evaluate("""() => {
        const grid = document.getElementById('ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1');
        if (!grid) return [];
        const results = [];
        grid.querySelectorAll('td > table.nested').forEach(nested => {
            const pk = nested.querySelector('input[id*="hdnPKValue"]');
            const info = nested.querySelector('.info');
            const textCell = nested.querySelector('td[colspan]');
            if (!pk) return;
            // Parse date from info text e.g. "Alabama Messenger | Monday, April 27, 2026 | County:"
            const infoText = info ? info.innerText : '';
            const dateMatch = infoText.match(/([A-Z][a-z]+day,\\s+[A-Z][a-z]+\\s+\\d+,\\s+\\d{4})/);
            // Newspaper name is in .info .left strong
            const pubEl = info ? info.querySelector('.left strong') : null;
            const newspaper = pubEl ? pubEl.textContent.trim() : '';
            // Detail URL query string from btnView2 onclick: "Details.aspx?SID=xxx&ID=yyy"
            const viewBtn = nested.querySelector('input[id*="btnView2"]');
            const onclick = viewBtn ? (viewBtn.getAttribute('onclick') || '') : '';
            const detailMatch = onclick.match(/location\\.href='Details\\.aspx\\?([^']+)'/);
            const detailQuery = detailMatch ? detailMatch[1] : '';
            results.push({
                notice_id: pk.value,
                pub_date_raw: dateMatch ? dateMatch[1] : '',
                snippet: textCell ? textCell.innerText.trim().substring(0, 2000) : '',
                newspaper: newspaper,
                detail_query: detailQuery,
            });
        });
        return results;
    }""")


# ── Per-Page Scraping ─────────────────────────────────────────────────


async def _parse_date_raw(raw: str) -> str:
    """Parse 'Monday, April 27, 2026' → '2026-04-27'."""
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _notice_from_snippet(
    row: dict,
    search: SearchConfig,
    session_base: str,
    pub_date: str,
) -> NoticeData:
    """Build a NoticeData from search-results snippet text (no CAPTCHA needed).

    Uses the snippet captured directly from the search results page.
    regex/LLM parsing runs later in the enrichment pipeline; we just need
    raw_text populated so filters can run and the parser has something to work with.
    """
    from notice_parser import _parse_address, _parse_name, _parse_auction_date

    notice_id = row["notice_id"]
    detail_q = row.get("detail_query") or f"SID={notice_id}"
    notice = NoticeData(
        county=search.county,
        notice_type=search.notice_type,
        # Pre-classified subtype (e.g. "unsafe_building" for code-violation
        # condemnation searches). Empty string when the search doesn't pre-tag.
        notice_subtype=getattr(search, "notice_subtype", "") or "",
        source_url=f"{session_base}DetailsPrint.aspx?{detail_q}",
        raw_text=row["snippet"],
        state="AL",
        date_added=pub_date,
        received_date=datetime.now().strftime("%Y-%m-%d"),
    )
    _parse_address(notice)
    _parse_name(notice)
    if search.notice_type != "probate":
        _parse_auction_date(notice)
    return notice


async def _scrape_notice(
    page: Page,
    notice_id: str,
    detail_query: str,
    session_base: str,
    search: SearchConfig,
    pub_date: str,
    since_date: str | None,
    llm_api_key: str | None,
    seen_ids: dict[str, str] | None,
    captcha_failed_ids: dict[str, dict] | None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> NoticeData | None:
    """Navigate to a single DetailsPrint.aspx, solve CAPTCHA, parse notice."""
    # Date cutoff check
    if since_date and pub_date and pub_date < since_date:
        logger.debug("  Skipping old notice %s (%s < %s)", notice_id, pub_date, since_date)
        return None

    # Cross-run dedup
    if seen_ids is not None and notice_id in seen_ids:
        logger.info("  Skipping already-processed notice ID=%s", notice_id)
        return None

    # Use the session-bearing query string captured from the results row onclick
    query = detail_query if detail_query else f"SID={notice_id}"
    detail_url = f"{session_base}DetailsPrint.aspx?{query}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
            await delay()

            if not await solve_captcha_and_view(page, rate_tracker=rate_tracker):
                logger.warning("  CAPTCHA solve failed for notice %s (attempt %d)", notice_id, attempt)
                if attempt >= MAX_RETRIES and captcha_failed_ids is not None:
                    captcha_failed_ids[notice_id] = {
                        "url": detail_url,
                        "search": search.search_terms,
                        "county": search.county,
                        "notice_type": search.notice_type,
                        "pub_date": pub_date,
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    }
                await delay()
                continue

            notice = await parse_notice_page(page, search.county, search.notice_type, llm_api_key, rate_tracker=rate_tracker)
            notice.source_url = detail_url
            if pub_date:
                notice.date_added = pub_date
            # Apply the search's pre-classified subtype (e.g. "unsafe_building"
            # for code-violation searches) only when the parser hasn't already
            # detected one. parse_notice_page auto-detects probate subtypes, so
            # this only fires for code-violation / future search types.
            search_subtype = getattr(search, "notice_subtype", "") or ""
            if search_subtype and not notice.notice_subtype:
                notice.notice_subtype = search_subtype

            if seen_ids is not None:
                seen_ids[notice_id] = notice.date_added or datetime.now().strftime("%Y-%m-%d")

            if not is_valid_foreclosure(notice):
                logger.debug("  Filtered out (not valid foreclosure): %s", notice_id)
                return None
            if not await is_target_county_async(
                notice.raw_text, search.county, api_key=llm_api_key,
            ):
                logger.debug("  Filtered out (wrong county): %s", notice_id)
                return None

            # Statewide catch-all searches (search.county="Statewide") pick
            # up notices whose actual property county isn't in the search
            # keywords. Reassign notice.county to the county the text
            # actually references so downstream DataSift Lists/tags land
            # in the right per-county filter presets. No-op when the
            # scrape's nominal county already matches the detected one.
            if assign_target_county_from_text(notice):
                logger.debug(
                    "  Reassigned notice.county %s → %s (from %s search)",
                    search.county, notice.county, search.search_terms,
                )

            logger.debug("  Kept notice %s", notice_id)
            return notice

        except PwTimeout:
            logger.warning("  Timeout on notice %s (attempt %d/%d)", notice_id, attempt, MAX_RETRIES)
            await delay()
        except Exception:
            logger.exception("  Error on notice %s (attempt %d/%d)", notice_id, attempt, MAX_RETRIES)
            await delay()

    return None


# ── Search Execution ──────────────────────────────────────────────────


async def run_search(
    page: Page,
    search: SearchConfig,
    since_date: str | None = None,
    llm_api_key: str | None = None,  # reserved: used when CAPTCHA fallback is re-enabled
    on_page_batch=None,
    start_page: int = 1,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
    captcha_failed_ids: dict[str, dict] | None = None,  # reserved: CAPTCHA fallback queue
    snippet_dropped_ids: set[str] | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> list[NoticeData]:
    """Submit search form, paginate through all result pages, scrape each notice."""
    days_back = search.days_back
    if since_date:
        # Compute days_back from since_date for the search form
        try:
            delta = (datetime.now() - datetime.strptime(since_date, "%Y-%m-%d")).days + 1
            days_back = max(1, delta)
        except ValueError:
            pass

    if not await _submit_search(page, search, days_back):
        return []

    session_base = _get_session_base(page.url)
    current_page, total_pages = await _get_page_info(page)
    logger.info("  %d pages of results for '%s'", total_pages, search.search_terms)

    # Skip to start_page if needed
    if start_page > 1:
        logger.info("  Skipping to page %d", start_page)
        while current_page < start_page:
            next_btn = await page.query_selector(SEL_NEXT_PAGE_BUTTON)
            if not next_btn:
                logger.error("  Cannot reach page %d — no next button", start_page)
                return []
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                await next_btn.click()
            await delay()
            current_page, total_pages = await _get_page_info(page)

    notices: list[NoticeData] = []

    while True:
        logger.info("  Scraping results page %d/%d", current_page, total_pages)

        # Collect all notice IDs and metadata from this results page
        rows = await _extract_row_data(page)
        logger.info("  %d notices on this results page", len(rows))

        # Remember the search URL to return to after detail pages
        search_url_with_session = page.url
        page_notices: list[NoticeData] = []

        for row in rows:
            notice_id = row["notice_id"]
            pub_date = await _parse_date_raw(row["pub_date_raw"])

            # Date + dedup pre-checks (avoid CAPTCHA cost)
            if since_date and pub_date and pub_date < since_date:
                logger.debug("  Skipping old notice %s (%s)", notice_id, pub_date)
                continue
            if seen_ids is not None and notice_id in seen_ids:
                logger.info("  Skipping already-processed notice ID=%s", notice_id)
                continue
            # P2 #7: persistent snippet-drop cache. The same statewide
            # query (e.g. probate "Estate Deceased") runs 3x — once per
            # county-labeled SearchConfig — and without persistence each
            # run re-pays the snippet filter check on the same non-target
            # IDs. Free per-row but adds ~5 min wall time per re-run.
            # Skip rows we've already dropped at snippet level in a prior
            # search this session.
            if snippet_dropped_ids is not None and notice_id in snippet_dropped_ids:
                continue

            # Snippet pre-filter: notice type appears in first line — cheap check
            # before paying CAPTCHA cost. Snippets are 2KB and DO contain
            # courthouse/county markers for probate + many other notice types.
            snippet_notice = _notice_from_snippet(row, search, session_base, pub_date)

            # 1) County pre-filter: drops notices for non-target Alabama
            #    counties without paying CAPTCHA. Critical for statewide
            #    queries like probate "Estate Deceased" (~1,000 results /
            #    14-day window across all 67 AL counties, of which ~80% are
            #    non-target). See notice_parser.snippet_passes_county_filter
            #    for the decision rules.
            if not snippet_passes_county_filter(row.get("snippet", "") or ""):
                logger.debug("  Snippet pre-filter: non-target AL county %s", notice_id)
                if snippet_dropped_ids is not None:
                    snippet_dropped_ids.add(notice_id)
                continue

            # 2) Foreclosure-specific validity check (skip notices whose
            #    snippet doesn't contain trustee-sale language even when the
            #    keyword search matched something incidental).
            if not is_valid_foreclosure(snippet_notice):
                logger.debug("  Snippet pre-filter: not valid foreclosure %s", notice_id)
                continue

            # Full detail page: navigate, solve CAPTCHA, parse complete notice text
            notice = await _scrape_notice(
                page,
                notice_id,
                row.get("detail_query", ""),
                session_base,
                search,
                pub_date,
                since_date,
                llm_api_key,
                seen_ids,
                captcha_failed_ids,
                rate_tracker=rate_tracker,
            )
            if notice is None:
                continue

            logger.debug("  Kept notice %s", notice_id)
            page_notices.append(notice)

            if max_notices and (len(notices) + len(page_notices)) >= max_notices:
                break

        notices.extend(page_notices)
        if on_page_batch and page_notices:
            await on_page_batch(page_notices)

        if max_notices and len(notices) >= max_notices:
            logger.info("  Reached max_notices limit (%d)", max_notices)
            notices = notices[:max_notices]
            break

        if current_page >= total_pages:
            break

        # Return to search results and go to next page.
        #
        # The cached `search_url_with_session` embeds an ASP.NET session GUID
        # captured BEFORE we visited any detail pages. After ~10 detail-page
        # CAPTCHA visits the AL site session may have rotated server-side, in
        # which case goto() returns 200 OK but loads a stale/empty page that
        # doesn't show the results grid. The previous code only fell through
        # to resubmit if goto() raised — silent stale-page loads broke
        # pagination for high-volume searches (probate, where most notices
        # filter out before we ever advance pages).
        #
        # Fix: validate that the results grid is actually present after goto.
        # If not, force the resubmit-and-click-to-page recovery path.
        need_resubmit = False
        try:
            await page.goto(search_url_with_session, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(1500)
            grid = await page.query_selector(SEL_RESULTS_GRID)
            if not grid:
                logger.info("  Cached search URL no longer shows results grid — resubmitting")
                need_resubmit = True
        except Exception:
            logger.warning("  Could not return to search results — resubmitting search")
            need_resubmit = True

        if need_resubmit:
            if not await _submit_search(page, search, days_back):
                break
            # Click forward to return to where we were. After _submit_search
            # we're on page 1; advance through current_page-1 next-clicks so
            # the click below moves us to current_page+1.
            replay_page = 1
            for _ in range(current_page - 1):
                if not await _click_next_page(page, replay_page):
                    break
                replay_page += 1
                await delay()

        # Advance to the next page via the ASP.NET image button.
        if not await _click_next_page(page, current_page):
            break
        await delay()
        current_page, total_pages = await _get_page_info(page)
        session_base = _get_session_base(page.url)

    logger.info("  Found %d notices for '%s'", len(notices), search.search_terms)
    return notices


# ── State Tracking ────────────────────────────────────────────────────


def load_last_run_date() -> str | None:
    data = config.load_state(STATE_FILE)
    return data.get("last_run_date")


def save_last_run_date() -> None:
    config.save_state(STATE_FILE, {"last_run_date": datetime.now().strftime("%Y-%m-%d")})


def load_seen_ids() -> dict[str, str]:
    data = config.load_state(SEEN_IDS_FILE)
    if not data:
        return {}
    cutoff = (datetime.now() - timedelta(days=SEEN_IDS_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {nid: d for nid, d in data.items() if d >= cutoff}
    if len(pruned) < len(data):
        logger.info("Pruned %d seen IDs older than %d days", len(data) - len(pruned), SEEN_IDS_PRUNE_DAYS)
    return pruned


def save_seen_ids(seen: dict[str, str]) -> None:
    config.save_state(SEEN_IDS_FILE, seen)


def load_captcha_failed_ids() -> dict[str, dict]:
    data = config.load_state(CAPTCHA_FAILED_IDS_FILE)
    if not data:
        return {}
    cutoff = (datetime.now() - timedelta(days=CAPTCHA_FAILED_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {
        nid: meta for nid, meta in data.items()
        if isinstance(meta, dict) and meta.get("first_seen", "") >= cutoff
    }
    if len(pruned) < len(data):
        logger.info("Pruned %d CAPTCHA-failed IDs older than %d days", len(data) - len(pruned), CAPTCHA_FAILED_PRUNE_DAYS)
    return pruned


def save_captcha_failed_ids(failed: dict[str, dict]) -> None:
    config.save_state(CAPTCHA_FAILED_IDS_FILE, failed)


# ── Main Entry Point ─────────────────────────────────────────────────


async def scrape_all(
    mode: str = "daily",
    searches: list[SearchConfig] | None = None,
    proxy_url: str | None = None,
    on_batch=None,
    since_date_override: str | None = None,
    llm_api_key: str | None = None,
    start_page: int = 1,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
    captcha_failed_ids: dict[str, dict] | None = None,
    on_search_complete=None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> list[NoticeData]:
    """Main entry point for scraping Alabama Public Notices.

    No login required. Submits keyword search form, paginates results,
    solves reCAPTCHA on each detail page.
    """
    if searches is None:
        searches = SAVED_SEARCHES

    if seen_ids is None:
        seen_ids = load_seen_ids()
    logger.info("Cross-run dedup: %d previously-seen notice IDs loaded", len(seen_ids))

    if captcha_failed_ids is None:
        captcha_failed_ids = load_captcha_failed_ids()
    prior_failed = len(captcha_failed_ids)
    if prior_failed:
        logger.info("CAPTCHA failure queue: %d IDs from prior runs still pending", prior_failed)

    # Determine date cutoff
    since_date: str | None = None
    if since_date_override:
        since_date = since_date_override
        logger.info("Using since_date override: %s", since_date)
    elif mode == "daily":
        since_date = load_last_run_date()
        if since_date:
            logger.info("Daily mode: pulling notices since %s", since_date)
        else:
            logger.info("Daily mode: no previous run found, pulling last 7 days")
            since_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    elif mode == "historical":
        since_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        logger.info("Historical mode: pulling notices since %s", since_date)

    all_notices: list[NoticeData] = []
    # Snippet-drop cache: persists across SearchConfig iterations within
    # this scrape_all call. Multiple SearchConfigs with identical
    # search_terms (e.g. "Estate Deceased" probate across Jefferson +
    # Madison + Marshall county labels) all surface the same statewide
    # result set; without this cache each re-pays the snippet pre-filter.
    snippet_dropped_ids: set[str] = set()

    async with async_playwright() as p:
        launch_opts: dict = {"headless": True}
        if proxy_url:
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_cfg: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_cfg["username"] = parsed.username
            if parsed.password:
                proxy_cfg["password"] = parsed.password
            launch_opts["proxy"] = proxy_cfg
            logger.info("Using proxy: %s:%s", parsed.hostname, parsed.port)

        browser = await p.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        context.set_default_timeout(60_000)
        page = await context.new_page()

        for search in searches:
            remaining = (max_notices - len(all_notices)) if max_notices else 0
            try:
                search_notices = await run_search(
                    page, search, since_date, llm_api_key,
                    on_page_batch=on_batch, start_page=start_page,
                    max_notices=remaining, seen_ids=seen_ids,
                    captcha_failed_ids=captcha_failed_ids,
                    snippet_dropped_ids=snippet_dropped_ids,
                    rate_tracker=rate_tracker,
                )
                all_notices.extend(search_notices)
            except Exception:
                logger.exception("Failed to scrape: %s", search.search_terms)

            try:
                save_seen_ids(seen_ids)
                if mode == "daily":
                    save_last_run_date()
                if on_search_complete is not None:
                    await on_search_complete(seen_ids)
            except Exception:
                logger.exception("Failed to persist seen_ids after %s", search.search_terms)

            if max_notices and len(all_notices) >= max_notices:
                logger.info("Reached max_notices limit (%d)", max_notices)
                break

        await browser.close()

    if mode == "daily":
        save_last_run_date()
    save_seen_ids(seen_ids)

    save_captcha_failed_ids(captcha_failed_ids)
    new_failed = len(captcha_failed_ids) - prior_failed
    if new_failed > 0:
        by_search: dict[str, int] = {}
        for meta in captcha_failed_ids.values():
            if not isinstance(meta, dict):
                continue
            s = meta.get("search", "unknown")
            by_search[s] = by_search.get(s, 0) + 1
        breakdown = ", ".join(f"{s}: {c}" for s, c in sorted(by_search.items()))
        logger.warning(
            "CAPTCHA DROPOUT: %d new notice(s) failed all retries this run "
            "(total queue: %d). Breakdown: %s. See captcha_failed_ids.json.",
            new_failed, len(captcha_failed_ids), breakdown,
        )

    logger.info("Total notices scraped: %d", len(all_notices))
    return all_notices
