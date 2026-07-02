"""2Captcha integration for solving reCAPTCHA v2 on alabamapublicnotices.com.

Every notice detail page (Details.aspx) on alabamapublicnotices.com (Jefferson,
Madison, Marshall — the active AL pipelines) requires solving a reCAPTCHA v2
checkbox before the full notice text is revealed. Flow:
  1. Verify we're on the detail page (View Notice button exists)
  2. Send websiteURL + sitekey to 2Captcha API
  3. 2Captcha returns a g-recaptcha-response token (~10-30s)
  4. Inject token into the page's hidden textarea
  5. Click "View Notice" button to submit
  6. Verify the notice content is now visible
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from playwright.async_api import Page, TimeoutError as PwTimeout
from twocaptcha import TwoCaptcha

import config
from config import MAX_RETRIES, RECAPTCHA_SITEKEY, SEL_VIEW_NOTICE_BUTTON

if TYPE_CHECKING:
    from observability import ServiceRateTracker

logger = logging.getLogger(__name__)


async def solve_captcha_and_view(
    page: Page,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> bool:
    """Solve reCAPTCHA v2 and click View Notice to reveal the full text.

    Retries up to MAX_RETRIES times on failure.
    Returns True if the notice text is now visible, False otherwise.

    Per CONTEXT.md D-04 (2Captcha success semantics):
      - success = solved on any of the 3 attempts (View Notice button cleared
        the gate)
      - failure = exhausted MAX_RETRIES without clearing the gate
      - IP-block bailout is NOT a 2Captcha failure (no record() call)
      - "Content already visible — no CAPTCHA needed" path is NOT a 2Captcha
        call either (no record() call) — the service was never invoked
    """
    if not config.CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set — cannot solve CAPTCHA")
        return False

    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Check for IP block message before wasting time on CAPTCHA
            block_msg = await page.query_selector(
                "text='You are not permitted to view public notices'"
            )
            if block_msg:
                logger.error(
                    "IP BLOCKED: Site says 'not permitted to view' — "
                    "need residential proxy or different IP"
                )
                return False  # Bail immediately, don't retry

            # Check if the notice content is already visible (no CAPTCHA needed)
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.info("Notice content already visible — no CAPTCHA needed")
                return True

            # Wait for the View Notice button to confirm we're on the detail page.
            # This prevents wasting CAPTCHA solves if the page didn't load properly.
            try:
                view_btn = await page.wait_for_selector(
                    SEL_VIEW_NOTICE_BUTTON, timeout=15000
                )
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            # Solve reCAPTCHA v2 via 2Captcha API (~10-30s)
            logger.warning(
                "Solving reCAPTCHA for %s (attempt %d/%d)", page_url, attempt, MAX_RETRIES
            )
            solver = TwoCaptcha(config.CAPTCHA_API_KEY)
            result = solver.recaptcha(
                sitekey=RECAPTCHA_SITEKEY,
                url=page_url,
            )
            token = result.get("code") if isinstance(result, dict) else str(result)

            if not token:
                logger.warning("2Captcha returned empty token (attempt %d)", attempt)
                continue

            # Inject the token into the page's hidden reCAPTCHA response field
            await page.evaluate(
                """(token) => {
                    const el = document.getElementById('g-recaptcha-response');
                    if (el) { el.value = token; el.style.display = 'block'; }
                    const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (ta) { ta.value = token; ta.style.display = 'block'; }
                    // Trigger the reCAPTCHA callback if it exists
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        const clients = ___grecaptcha_cfg.clients;
                        if (clients) {
                            Object.keys(clients).forEach(key => {
                                const client = clients[key];
                                const findCallback = (obj) => {
                                    if (!obj || typeof obj !== 'object') return;
                                    Object.values(obj).forEach(v => {
                                        if (typeof v === 'object' && v !== null) {
                                            if (typeof v.callback === 'function') {
                                                v.callback(token);
                                            }
                                            findCallback(v);
                                        }
                                    });
                                };
                                findCallback(client);
                            });
                        }
                    }
                }""",
                token,
            )

            # Brief pause for any callback-triggered actions
            await asyncio.sleep(1)

            # Click the "View Notice" button to submit with the solved CAPTCHA.
            # Re-find the button in case the callback caused a DOM update.
            view_btn = await page.query_selector(SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                # Callback may have auto-submitted — check if content is visible
                content_el = await page.query_selector("text='Notice Content'")
                if content_el:
                    logger.warning("CAPTCHA solved — callback auto-submitted form")
                    if rate_tracker is not None:
                        rate_tracker.record("2captcha", True)
                    return True
                logger.warning("View Notice button gone after token inject (attempt %d)", attempt)
                continue

            await view_btn.click()

            # 2026-07-01 fix v2: prior code did `wait_for_load_state(
            # "networkidle")` with the default 60s timeout. Started failing
            # 100% of the time on today's cron (0/132 successes over 4 hours
            # = workflow cancelled). Site now does background network
            # activity (tracking / long-poll) that keeps the network active
            # indefinitely, so networkidle NEVER fires.
            #
            # v1 fix (bug — introduced later on 2026-07-01) replaced the
            # networkidle wait with a 10s wait_for_selector on "Notice
            # Content", then fell through to a "gate cleared" fallback that
            # returned SUCCESS if the CAPTCHA message was gone. This
            # SILENTLY MASKED text-not-rendered failures: 237/237 solves
            # fired "gate cleared" (not "notice text visible") → every
            # notice's raw_text was empty → LLM extracted address='' → tier
            # filter dropped ALL 213 as no-ZIP.
            #
            # v2 fix (this):
            #   1. Bump wait_for_selector timeout to 45s — the site takes
            #      >10s to render "Notice Content" post-CAPTCHA in the
            #      current version (empirically observed 2026-07-01).
            #   2. Do NOT report "gate cleared" as success. If we can't
            #      see "Notice Content", the notice text isn't rendered,
            #      and downstream parsing WILL fail. Better to retry (or
            #      let the caller drop the notice) than pretend it worked.
            try:
                await page.wait_for_selector(
                    "text='Notice Content'", timeout=45000,
                )
            except Exception:
                pass  # will re-check with query_selector below

            # Verify the notice content is now visible — HARD REQUIREMENT.
            # If this fails, the CAPTCHA gate may be gone but the notice
            # text hasn't loaded, so we can't parse anything downstream.
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.warning("CAPTCHA solved — notice text visible")
                if rate_tracker is not None:
                    rate_tracker.record("2captcha", True)
                return True

            # Give it one last nudge — sometimes the DOM is 500ms behind
            # the network signal.
            await page.wait_for_timeout(2000)
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.warning(
                    "CAPTCHA solved — notice text visible (after 2s nudge)"
                )
                if rate_tracker is not None:
                    rate_tracker.record("2captcha", True)
                return True

            # Content still not visible. Check what state we're in for the
            # log — either the CAPTCHA gate is still up (retry needed) or
            # cleared-but-content-missing (site error, retry likely same).
            captcha_msg = await page.query_selector(
                "text='You must complete the reCAPTCHA'"
            )
            if captcha_msg:
                logger.warning(
                    "CAPTCHA still present after attempt %d", attempt,
                )
            else:
                # Gate cleared but no notice text — used to be treated as
                # success (v1 bug). Now we log and retry so we don't feed
                # the parser an empty page.
                logger.warning(
                    "CAPTCHA gate cleared but 'Notice Content' not visible "
                    "after 45s+nudge (attempt %d) — treating as failure",
                    attempt,
                )

        except Exception:
            logger.exception("CAPTCHA solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("All %d CAPTCHA attempts failed for %s", MAX_RETRIES, page_url)
    if rate_tracker is not None:
        rate_tracker.record("2captcha", False)
    return False
