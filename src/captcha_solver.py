"""Cloudflare Turnstile integration for solving CAPTCHAs on
alabamapublicnotices.com.

MIGRATION (2026-07-02, v3): alabamapublicnotices.com switched from Google
reCAPTCHA v2 to Cloudflare Turnstile sometime between 2026-06-30 (last
successful cron run) and 2026-07-01 (broke). Evidence from headed-mode
probe on 2026-07-02:
  - Response field: cf-turnstile-response (was g-recaptcha-response)
  - Widget: <div class="cf-turnstile" data-sitekey="0x4AAAAAAD..." id="recaptcha">
    (the id="recaptcha" is a legacy label; the actual widget is Turnstile)
  - Turnstile API script loaded: challenges.cloudflare.com/turnstile/v0/api.js
  - New gate message: "You must complete the CAPTCHA in order to continue."
    (was "You must complete the reCAPTCHA")
  - Button label: "I Agree, View Notice" (was just "View Notice") — button
    ID unchanged, so SEL_VIEW_NOTICE_BUTTON still valid.

The v1 fix (07-01, commit 92dfe97) and v2 fix (07-01, commit c1cbe83) both
targeted the wrong CAPTCHA provider — 2Captcha was returning reCAPTCHA
tokens that the site (validating Turnstile tokens) silently rejected. The
"gate cleared" success signal in v1 was fooled by the new gate message
text not matching the old string; v2 correctly refused to claim success
but never got a real one because the token itself was wrong-provider.

Flow (v3):
  1. Verify we're on the detail page (View Notice button exists)
  2. Extract Turnstile sitekey from the page's data-sitekey attribute
     (fall back to config.TURNSTILE_SITEKEY_FALLBACK if the attribute is
     missing — happens if the widget hasn't hydrated yet)
  3. Send URL + sitekey to 2Captcha's turnstile solver
  4. Inject the token into cf-turnstile-response
  5. Click "I Agree, View Notice" button to submit
  6. Wait up to 45s for the notice content to render
  7. Confirm via either "Notice Content" text (legacy marker, may or may
     not still exist post-migration) OR substantial body content length
     (>1500 chars, matches historical average notice size)
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from playwright.async_api import Page, TimeoutError as PwTimeout
from twocaptcha import TwoCaptcha

import config
from config import (
    MAX_RETRIES,
    SEL_VIEW_NOTICE_BUTTON,
    TURNSTILE_SITEKEY_FALLBACK,
)

if TYPE_CHECKING:
    from observability import ServiceRateTracker

logger = logging.getLogger(__name__)

# Minimum body text length to consider the notice content "rendered".
# Empirical: pre-CAPTCHA pages are ~450 chars (Terms of Use + gate
# message); post-CAPTCHA pages with real notice content are typically
# 2000-20000 chars. 1500 is a safe threshold that catches even the
# shortest legitimate notices while cleanly rejecting the gate-only page.
MIN_CONTENT_CHARS = 1500


async def _extract_turnstile_sitekey(page: Page) -> str:
    """Read the Turnstile sitekey from the widget's data-sitekey attribute.

    Falls back to the hardcoded default when the attribute is missing —
    happens rarely if the widget hasn't fully hydrated by the time we
    check, or if the site changes markup again.
    """
    try:
        widget = await page.query_selector(".cf-turnstile[data-sitekey]")
        if widget:
            sitekey = await widget.get_attribute("data-sitekey")
            if sitekey and sitekey.startswith("0x"):
                return sitekey
    except Exception as e:
        logger.debug("Sitekey extraction via selector failed: %s", e)

    logger.debug(
        "Turnstile widget data-sitekey not found — using fallback %s",
        TURNSTILE_SITEKEY_FALLBACK,
    )
    return TURNSTILE_SITEKEY_FALLBACK


async def solve_captcha_and_view(
    page: Page,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> bool:
    """Solve Cloudflare Turnstile and click View Notice to reveal the full
    notice text.

    Retries up to MAX_RETRIES times on failure. Returns True if the notice
    text is now visible, False otherwise.

    Per CONTEXT.md D-04 (2Captcha success semantics):
      - success = solved on any of the 3 attempts (View Notice button
        cleared the gate AND notice content rendered)
      - failure = exhausted MAX_RETRIES without clearing the gate
      - IP-block bailout is NOT a 2Captcha failure (no record() call)
      - "Content already visible — no CAPTCHA needed" path is NOT a
        2Captcha call either (no record() call) — the service was never
        invoked
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

            # Check if the notice content is already visible (no CAPTCHA
            # needed). Two-signal check because post-Turnstile the "Notice
            # Content" text marker may or may not still be present — trust
            # body length as the definitive indicator.
            body_text = await page.inner_text("body")
            if (await page.query_selector("text='Notice Content'")
                    or len(body_text) >= MIN_CONTENT_CHARS):
                logger.info(
                    "Notice content already visible (%d chars) — no CAPTCHA needed",
                    len(body_text),
                )
                return True

            # Wait for the View Notice button to confirm we're on the
            # detail page. This prevents wasting CAPTCHA solves if the
            # page didn't load properly.
            try:
                await page.wait_for_selector(
                    SEL_VIEW_NOTICE_BUTTON, timeout=15000,
                )
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s "
                    "(attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            # Extract Turnstile sitekey from the widget (fresh per attempt
            # in case the widget re-renders between retries).
            sitekey = await _extract_turnstile_sitekey(page)

            # Solve Turnstile via 2Captcha API (~10-30s)
            logger.warning(
                "Solving Turnstile for %s (attempt %d/%d, sitekey=%s)",
                page_url, attempt, MAX_RETRIES, sitekey[:12] + "...",
            )
            solver = TwoCaptcha(config.CAPTCHA_API_KEY)
            result = solver.turnstile(sitekey=sitekey, url=page_url)
            token = (
                result.get("code") if isinstance(result, dict)
                else str(result)
            )
            if not token:
                logger.warning(
                    "2Captcha returned empty token (attempt %d)", attempt,
                )
                continue

            # Inject the token into the page's Turnstile response field.
            # The field is created by Turnstile's widget hydration and is
            # named cf-turnstile-response (previously g-recaptcha-response
            # for the old Google reCAPTCHA setup).
            #
            # Turnstile's widget also has a JS callback pattern via
            # `turnstile.execute()` but we skip that in favor of directly
            # setting the input value + relying on the ASP.NET form's
            # submit-on-click flow. The site's server-side handler reads
            # cf-turnstile-response from the POST body regardless of how
            # the value got there.
            await page.evaluate(
                """(token) => {
                    // Primary: the hidden input the widget populates
                    const input = document.querySelector(
                        'input[name="cf-turnstile-response"]'
                    );
                    if (input) {
                        input.value = token;
                    }
                    // Belt-and-suspenders: any other Turnstile-related
                    // input/textarea (some Turnstile modes create both).
                    document.querySelectorAll(
                        '[name="cf-turnstile-response"], '
                        + '[id^="cf-chl-widget"]'
                    ).forEach(el => {
                        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                            el.value = token;
                        }
                    });
                }""",
                token,
            )

            # Brief pause for any JS handlers to run
            await asyncio.sleep(1)

            # Click "I Agree, View Notice" to submit the form. Button ID
            # unchanged from the pre-migration site so SEL_VIEW_NOTICE_BUTTON
            # still targets the right element (label text changed from
            # "View Notice" to "I Agree, View Notice" — ID stayed the same).
            view_btn = await page.query_selector(SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                # In rare cases the callback auto-submits the form; check
                # if content is already visible before giving up on this
                # attempt.
                body_text_check = await page.inner_text("body")
                if len(body_text_check) >= MIN_CONTENT_CHARS:
                    logger.warning(
                        "CAPTCHA solved — form auto-submitted (%d chars)",
                        len(body_text_check),
                    )
                    if rate_tracker is not None:
                        rate_tracker.record("2captcha", True)
                    return True
                logger.warning(
                    "View Notice button gone after token inject (attempt %d)",
                    attempt,
                )
                continue

            await view_btn.click()

            # Wait up to 45s for the notice content to render. Use a
            # content-length signal: pre-solve pages are ~450 chars (Terms
            # + gate message), post-solve pages are 2000-20000 chars. If
            # body length exceeds MIN_CONTENT_CHARS we've solved it.
            #
            # We also fall through to check for the legacy "Notice Content"
            # text marker — some notices may still render it, and it's a
            # cleaner signal when present. But we don't REQUIRE it because
            # the migration may have removed it.
            SUCCESS_POLL_INTERVAL_MS = 2000
            DEADLINE_MS = 45000
            elapsed_ms = 0
            content_rendered = False
            while elapsed_ms < DEADLINE_MS:
                # Check both signals: length + legacy marker
                body_text = await page.inner_text("body")
                if len(body_text) >= MIN_CONTENT_CHARS:
                    content_rendered = True
                    break
                if await page.query_selector("text='Notice Content'"):
                    body_text = await page.inner_text("body")
                    content_rendered = True
                    break
                await page.wait_for_timeout(SUCCESS_POLL_INTERVAL_MS)
                elapsed_ms += SUCCESS_POLL_INTERVAL_MS

            if content_rendered:
                logger.warning(
                    "Turnstile solved — notice content rendered "
                    "(%d chars body text)",
                    len(body_text),
                )
                if rate_tracker is not None:
                    rate_tracker.record("2captcha", True)
                return True

            # Content still not rendered after 45s. Distinguish gate-still-
            # up (retry needed — new token might work) from gate-cleared-
            # but-content-missing (site error, retry unlikely to help but
            # try anyway per MAX_RETRIES).
            new_gate_msg = await page.query_selector(
                "text='You must complete the CAPTCHA'"
            )
            # Legacy check kept for defense-in-depth — if the site rolls
            # back to reCAPTCHA the old message would reappear.
            legacy_gate_msg = await page.query_selector(
                "text='You must complete the reCAPTCHA'"
            )
            if new_gate_msg or legacy_gate_msg:
                gate_type = "Turnstile" if new_gate_msg else "reCAPTCHA"
                logger.warning(
                    "%s gate still present after attempt %d — token may "
                    "have been rejected",
                    gate_type, attempt,
                )
            else:
                logger.warning(
                    "Gate cleared but body text still short (%d < %d "
                    "chars) after 45s (attempt %d) — treating as failure",
                    len(body_text), MIN_CONTENT_CHARS, attempt,
                )

        except Exception:
            logger.exception(
                "CAPTCHA solve error (attempt %d/%d)", attempt, MAX_RETRIES,
            )

    logger.error(
        "All %d CAPTCHA attempts failed for %s", MAX_RETRIES, page_url,
    )
    if rate_tracker is not None:
        rate_tracker.record("2captcha", False)
    return False
