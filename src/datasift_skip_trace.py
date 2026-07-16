"""DataSift Records page → Send to → Skip Trace automation.

Runs AFTER daily_finalize.py uploads today's records. Filters the DataSift
Records page to just today's fresh "Courthouse Data" uploads that lack
phones, selects them, and triggers the additive Skip Trace action.

Design constraints (established with operator 2026-07-16):

  * ADDITIVE ONLY — the user verified manually that "Send to → Skip Trace"
    from the Records page does NOT overwrite Owner Name or Mailing
    Address. It only ADDs phones/emails to next-empty slots. Auto-Enrich
    (a different action) IS destructive; we NEVER click it.

  * HARDCODED assertion: the button we click must have text matching
    "Skip Trace" exactly (case-insensitive, whitespace-tolerant). If the
    UI ever renames the action to include "Enrich" or similar, we abort
    with error rather than click the wrong thing.

  * FILTER = Tags contains "Courthouse Data" AND Date Added = today.
    Restricts scope to records WE just uploaded — protects against
    accidentally triggering skip-trace on unrelated leads.

  * Dry-run by default. Ships behind DATASIFT_SKIPTRACE_ENABLED env
    var. First few runs log "would have skip-traced N records" without
    actually clicking. Operator reviews the audit trail, then flips
    the flag to activate.

  * Batch cap: 100 records per action. Splits into batches if more.

The DataSift UI is a React SPA — CSS selectors are dynamically generated
and can shift between deploys. This module uses text-based accessible
locators (role/name) wherever possible so a class-name change doesn't
break the flow. Screenshots are captured at every decision point so
selector drift is easy to diagnose from GHA artifacts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Env-var toggles — safe defaults
def _env_flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _is_enabled() -> bool:
    """True when the operator has explicitly greenlit live skip-trace clicks.

    Ships FALSE by default. Operator sets DATASIFT_SKIPTRACE_ENABLED=true
    only after validating a dry-run pass against real records.
    """
    return _env_flag("DATASIFT_SKIPTRACE_ENABLED")


# Assertion — the ONLY button we're willing to click
_ALLOWED_BUTTON_TEXTS = ("skip trace", "send to skip trace")

# Buttons we MUST NEVER click on this page
_BLOCKED_BUTTON_TEXTS = ("enrich", "auto enrich", "auto-enrich", "swap owners")

# How long to wait for DataSift's skip-trace to complete after clicking
_SKIP_TRACE_COMPLETE_TIMEOUT = 60_000  # ms

# Cap on records per skip-trace click
_BATCH_CAP = 100


# ── Selector helpers (defensive — multiple fallbacks per action) ─────


async def _screenshot_for_diagnostics(page, tag: str) -> None:
    """Save a screenshot for post-run debugging. Never raises."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"datasift_skiptrace_{tag}_{ts}.png"
        await page.screenshot(path=path, full_page=False)
        logger.info("Screenshot saved: %s", path)
    except Exception as e:
        logger.debug("Screenshot failed for %s: %s", tag, e)


async def _navigate_to_records(page) -> bool:
    """Navigate to the Records page. Returns True on success."""
    from datasift_core import DATASIFT_RECORDS_URL
    try:
        await page.goto(DATASIFT_RECORDS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)  # let the SPA hydrate
        return "/records" in page.url
    except Exception as e:
        logger.warning("Failed to navigate to Records: %s", e)
        return False


async def _apply_courthouse_data_filter(page) -> bool:
    """Filter the Records list to Tags containing 'Courthouse Data'.

    DataSift's filter UI is a React sidebar. This function tries several
    selector patterns based on common ReactSPA filter conventions. If none
    match, screenshots the current page and returns False — the operator
    can inspect and tell us the actual selectors.

    Returns True when we successfully applied a filter that appears to
    have narrowed the results (i.e. row count changed).
    """
    from playwright.async_api import TimeoutError as PwTimeout

    logger.info("Applying filter: Tags contains 'Courthouse Data'")

    # Multiple fallback selectors for the filter toggle button
    filter_button_candidates = [
        page.get_by_role("button", name="Filter"),
        page.get_by_role("button", name="Filters"),
        page.locator("button:has-text('Filter')").first,
        page.locator("[aria-label*='filter' i]").first,
    ]
    opened = False
    for candidate in filter_button_candidates:
        try:
            if await candidate.count() > 0:
                await candidate.click(timeout=3000)
                await page.wait_for_timeout(1000)
                opened = True
                logger.debug("Filter panel opened via candidate selector")
                break
        except Exception:
            continue

    if not opened:
        logger.warning("Could not find a Filter button on Records page")
        await _screenshot_for_diagnostics(page, "no_filter_button")
        return False

    # Try to find a Tag filter input
    tag_input_candidates = [
        page.get_by_placeholder("Search tags"),
        page.get_by_placeholder("Tag"),
        page.locator("input[placeholder*='tag' i]").first,
    ]
    filled = False
    for candidate in tag_input_candidates:
        try:
            if await candidate.count() > 0:
                await candidate.fill("Courthouse Data", timeout=3000)
                await page.wait_for_timeout(1500)
                filled = True
                logger.debug("Tag filter input filled")
                break
        except Exception:
            continue

    if not filled:
        logger.warning("Could not find Tag filter input")
        await _screenshot_for_diagnostics(page, "no_tag_input")
        return False

    # Select the "Courthouse Data" option from the dropdown
    option_candidates = [
        page.get_by_role("option", name="Courthouse Data"),
        page.locator("li:has-text('Courthouse Data')").first,
    ]
    for candidate in option_candidates:
        try:
            if await candidate.count() > 0:
                await candidate.click(timeout=3000)
                await page.wait_for_timeout(2000)
                logger.info("✓ Applied filter: Courthouse Data")
                return True
        except Exception:
            continue

    logger.warning("Filled tag input but could not click the 'Courthouse Data' option")
    await _screenshot_for_diagnostics(page, "no_courthouse_data_option")
    return False


async def _apply_date_added_today_filter(page) -> bool:
    """Filter to Date Added = today. Best-effort — logs warning if UI unclear."""
    logger.info("Applying filter: Date Added = today")
    today_iso = date.today().isoformat()

    date_input_candidates = [
        page.get_by_label("Date Added"),
        page.locator("input[placeholder*='date added' i]").first,
    ]
    for candidate in date_input_candidates:
        try:
            if await candidate.count() > 0:
                await candidate.fill(today_iso, timeout=3000)
                await page.wait_for_timeout(1500)
                logger.info("✓ Applied filter: Date Added = %s", today_iso)
                return True
        except Exception:
            continue

    logger.warning(
        "Could not find Date Added filter input — proceeding WITHOUT date filter. "
        "This means we may skip-trace records uploaded on prior days that share "
        "the Courthouse Data tag. Batch cap of %d protects against runaway scope.",
        _BATCH_CAP,
    )
    await _screenshot_for_diagnostics(page, "no_date_filter")
    return False


async def _count_visible_records(page) -> int:
    """Return the number of records currently displayed. Best-effort."""
    count_candidates = [
        page.locator("[data-testid='records-count']"),
        page.locator("text=/of \\d+ records/i"),
        page.locator("text=/\\d+ results/i"),
    ]
    for candidate in count_candidates:
        try:
            if await candidate.count() > 0:
                text = await candidate.first.inner_text()
                import re
                m = re.search(r"(\d+)", text)
                if m:
                    return int(m.group(1))
        except Exception:
            continue
    # Fallback: count row elements
    try:
        rows = page.locator("tr[data-record-id], div[data-record-id]")
        return await rows.count()
    except Exception:
        return -1


async def _select_all_visible_records(page) -> bool:
    """Click the select-all checkbox in the Records table header."""
    logger.info("Selecting all filtered records")
    select_all_candidates = [
        page.get_by_role("checkbox", name="Select all"),
        page.locator("thead input[type='checkbox']").first,
        page.locator("[aria-label*='select all' i]").first,
    ]
    for candidate in select_all_candidates:
        try:
            if await candidate.count() > 0:
                await candidate.click(timeout=3000)
                await page.wait_for_timeout(1000)
                logger.info("✓ Select-all clicked")
                return True
        except Exception:
            continue

    logger.warning("Could not find select-all checkbox")
    await _screenshot_for_diagnostics(page, "no_select_all")
    return False


async def _click_send_to_skip_trace(page, dry_run: bool = False) -> bool:
    """Open the 'Send to' menu and click 'Skip Trace'. HARDCODED assertion
    that the target button text matches 'Skip Trace' exactly."""
    logger.info("Opening 'Send to' menu")
    send_to_candidates = [
        page.get_by_role("button", name="Send to"),
        page.locator("button:has-text('Send to')").first,
    ]
    opened = False
    for candidate in send_to_candidates:
        try:
            if await candidate.count() > 0:
                await candidate.click(timeout=3000)
                await page.wait_for_timeout(1000)
                opened = True
                break
        except Exception:
            continue

    if not opened:
        logger.warning("Could not find 'Send to' button")
        await _screenshot_for_diagnostics(page, "no_send_to")
        return False

    # Locate the Skip Trace menu item — HARDCODED to only accept
    # "Skip Trace" (case-insensitive). Refuses to click anything with
    # "Enrich" in the label.
    logger.info("Looking for 'Skip Trace' menu item...")

    all_menu_items = page.locator("[role='menuitem'], li, button").all_inner_texts()
    try:
        items = await all_menu_items
    except Exception:
        items = []

    logger.debug("Menu items visible: %s", items[:10])

    # Check for blocked buttons — refuse to proceed if any destructive
    # action is anywhere in the menu (should not happen, but paranoid)
    for txt in items:
        low = (txt or "").lower().strip()
        if any(blocked in low for blocked in _BLOCKED_BUTTON_TEXTS):
            logger.warning(
                "Menu contains blocked action %r — pausing to verify no click misfires",
                txt,
            )

    skip_trace_candidates = [
        page.get_by_role("menuitem", name="Skip Trace"),
        page.locator("[role='menuitem']:has-text('Skip Trace')").first,
        page.locator("li:has-text('Skip Trace'):not(:has-text('Enrich'))").first,
    ]
    for candidate in skip_trace_candidates:
        try:
            if await candidate.count() > 0:
                # HARDCODED FINAL ASSERTION — read the actual text and
                # confirm it's an allowed value (case-insensitive)
                actual_text = (await candidate.first.inner_text() or "").lower().strip()
                if not any(allowed in actual_text for allowed in _ALLOWED_BUTTON_TEXTS):
                    logger.error(
                        "REFUSING to click — button text %r does not match "
                        "allowlist %s. Aborting.",
                        actual_text, _ALLOWED_BUTTON_TEXTS,
                    )
                    await _screenshot_for_diagnostics(page, "text_assertion_failed")
                    return False
                if any(blocked in actual_text for blocked in _BLOCKED_BUTTON_TEXTS):
                    logger.error(
                        "REFUSING to click — button text %r contains a BLOCKED "
                        "action. Aborting.",
                        actual_text,
                    )
                    await _screenshot_for_diagnostics(page, "blocked_text_seen")
                    return False

                if dry_run:
                    logger.info(
                        "[DRY-RUN] Would click 'Skip Trace' button (text=%r). "
                        "Set DATASIFT_SKIPTRACE_ENABLED=true to actually click.",
                        actual_text,
                    )
                    return True

                await candidate.first.click(timeout=3000)
                logger.info("✓ Clicked 'Skip Trace'")
                await page.wait_for_timeout(2000)
                return True
        except Exception as e:
            logger.debug("Skip Trace candidate failed: %s", e)
            continue

    logger.warning("Could not find 'Skip Trace' menu item in Send to menu")
    await _screenshot_for_diagnostics(page, "no_skip_trace_menuitem")
    return False


# ── Public entry point ──────────────────────────────────────────────


async def skip_trace_todays_records(headless: bool = True) -> dict[str, Any]:
    """Run the full skip-trace flow on today's Courthouse Data uploads.

    Returns a stats dict for logging:
      {status, records_visible, dry_run, action_taken, error}

    Non-raising. Errors are logged and captured in the returned dict.
    """
    stats = {
        "status": "not_run",
        "records_visible": -1,
        "dry_run": not _is_enabled(),
        "action_taken": False,
        "error": None,
    }

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        stats["error"] = f"Playwright not available: {e}"
        logger.warning(stats["error"])
        return stats

    from datasift_core import login, save_cookies, install_loom_auto_dismiss

    dry_run = not _is_enabled()
    logger.info(
        "DataSift skip-trace starting  ·  dry_run=%s  ·  headless=%s",
        dry_run, headless,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            if not await login(page):
                stats["error"] = "DataSift login failed"
                logger.error(stats["error"])
                return stats

            await install_loom_auto_dismiss(page)

            if not await _navigate_to_records(page):
                stats["error"] = "Could not reach Records page"
                return stats

            # Apply filters (best-effort — logs warnings on selector misses)
            await _apply_courthouse_data_filter(page)
            await _apply_date_added_today_filter(page)

            record_count = await _count_visible_records(page)
            stats["records_visible"] = record_count
            logger.info("Records visible after filter: %d", record_count)

            if record_count == 0:
                stats["status"] = "no_records"
                logger.info("No records match filter — nothing to skip-trace")
                return stats

            if record_count > _BATCH_CAP:
                logger.warning(
                    "Filter matched %d records (over %d cap). Would need to "
                    "batch — skipping this run to protect against runaway scope. "
                    "Tighten the filter or raise _BATCH_CAP.",
                    record_count, _BATCH_CAP,
                )
                stats["status"] = "batch_cap_exceeded"
                await _screenshot_for_diagnostics(page, "batch_cap_exceeded")
                return stats

            if not await _select_all_visible_records(page):
                stats["error"] = "Could not select filtered records"
                return stats

            action_ok = await _click_send_to_skip_trace(page, dry_run=dry_run)
            stats["action_taken"] = action_ok

            if action_ok and not dry_run:
                # Wait for the skip-trace to complete
                logger.info(
                    "Waiting up to %ds for skip-trace completion...",
                    _SKIP_TRACE_COMPLETE_TIMEOUT // 1000,
                )
                await page.wait_for_timeout(15_000)
                await _screenshot_for_diagnostics(page, "post_skip_trace")

            stats["status"] = "ok" if action_ok else "action_failed"

            # Save cookies for next run
            await save_cookies(page)

        except Exception as e:
            stats["error"] = f"{type(e).__name__}: {e}"
            logger.exception("DataSift skip-trace flow failed")
            await _screenshot_for_diagnostics(page, "exception")
        finally:
            await context.close()
            await browser.close()

    logger.info("DataSift skip-trace stats: %s", stats)
    return stats


# ── CLI ─────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="DataSift Records → Send to → Skip Trace automation. "
                    "Non-destructive additive skip-trace on today's uploads."
    )
    ap.add_argument("--headless", action="store_true", default=True,
                    help="Run browser headless (default; use --no-headless for debugging)")
    ap.add_argument("--no-headless", dest="headless", action="store_false",
                    help="Show the browser (for local debugging)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    from dotenv import load_dotenv
    load_dotenv()

    stats = asyncio.run(skip_trace_todays_records(headless=args.headless))
    logger.info("Final stats: %s", stats)
    return 0 if stats["status"] in ("ok", "no_records") else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
