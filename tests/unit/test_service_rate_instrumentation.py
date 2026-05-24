"""Service-rate instrumentation tests (Phase 2, OBS-01).

Each test exercises one of the 4 tracked services (2Captcha, Smarty, Tracerfy,
LLM) per CONTEXT.md D-04 failure semantics. The tests:

- Use unittest.mock / pytest monkeypatch — NO live API calls
- Instantiate a fresh ServiceRateTracker per test and assert on totals()
- Cover success-path, failure-path, and the no-op-when-tracker-is-None contract

See ``.planning/phases/02-funnel-transparency/02-02-PLAN.md`` for the full
plan and ``02-CONTEXT.md`` D-04 for the per-service success/failure
definitions.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from observability import ServiceRateTracker


# ── 2Captcha ───────────────────────────────────────────────────────────


def _make_awaitable_button() -> MagicMock:
    """Build a view-button mock whose ``click()`` is an awaitable coroutine."""
    btn = MagicMock()

    async def _click():
        return None

    btn.click = _click
    return btn


def _build_mock_captcha_page(
    *,
    has_view_button: bool = True,
    content_visible_after_solve: bool = True,
) -> MagicMock:
    """Build a Playwright Page mock for the captcha solve flow.

    The captcha_solver flow makes these calls per attempt (in order):
      1. query_selector('not permitted') — IP-block check → None
      2. query_selector('Notice Content') — pre-solve check → None
      3. wait_for_selector(view button, timeout=15000) — confirm detail page
      4. solver.recaptcha() — issued via patched TwoCaptcha (not on page)
      5. page.evaluate(injection JS) → None
      6. query_selector(view button) — re-find for click → truthy
      7. view_btn.click() — must be awaitable
      8. page.wait_for_load_state('networkidle') → None
      9. query_selector('Notice Content') — post-solve check → truthy on success
     10. (only on failure) query_selector('complete the reCAPTCHA') → None
    """
    page = MagicMock()
    page.url = "https://alabamapublicnotices.com/Details.aspx?id=1"
    page._content_query_count = 0

    async def query_selector(sel):
        if "not permitted" in sel:
            return None
        if "Notice Content" in sel:
            # First call: pre-solve check (always None — we need a solve).
            # Subsequent calls: per-attempt post-solve check (truthy when
            # the solve succeeded, None when it failed).
            page._content_query_count += 1
            if page._content_query_count == 1:
                return None
            return _make_awaitable_button() if content_visible_after_solve else None
        if "complete the reCAPTCHA" in sel:
            return None
        # SEL_VIEW_NOTICE_BUTTON re-query after token inject — return an
        # awaitable-click button so the post-inject click() works.
        return _make_awaitable_button() if has_view_button else None

    async def wait_for_selector(sel, timeout=15000):
        if has_view_button:
            return _make_awaitable_button()
        from playwright.async_api import TimeoutError as PwTimeout
        raise PwTimeout("View Notice button not found")

    async def evaluate(script, *args):
        return None

    async def wait_for_load_state(state):
        return None

    page.query_selector = query_selector
    page.wait_for_selector = wait_for_selector
    page.evaluate = evaluate
    page.wait_for_load_state = wait_for_load_state
    return page


def test_captcha_records_success_on_clean_solve(monkeypatch):
    """First-attempt clean solve: tracker records exactly one success."""
    import config as cfg
    import captcha_solver

    monkeypatch.setattr(cfg, "CAPTCHA_API_KEY", "fake-key", raising=False)

    fake_solver = MagicMock()
    fake_solver.recaptcha.return_value = {"code": "TOKEN_ABC"}
    monkeypatch.setattr(captcha_solver, "TwoCaptcha", lambda key: fake_solver)

    page = _build_mock_captcha_page(content_visible_after_solve=True)
    tracker = ServiceRateTracker()

    result = asyncio.run(
        captcha_solver.solve_captcha_and_view(page, rate_tracker=tracker)
    )

    assert result is True
    assert tracker.totals()["2captcha"] == {"success": 1, "total": 1}


def test_captcha_records_failure_on_exhausted_retries(monkeypatch):
    """All MAX_RETRIES attempts fail → tracker records exactly one failure."""
    import config as cfg
    import captcha_solver

    monkeypatch.setattr(cfg, "CAPTCHA_API_KEY", "fake-key", raising=False)

    fake_solver = MagicMock()
    # Empty token = each attempt fails. Loop logs "All ... attempts failed".
    fake_solver.recaptcha.return_value = {"code": ""}
    monkeypatch.setattr(captcha_solver, "TwoCaptcha", lambda key: fake_solver)

    page = _build_mock_captcha_page(content_visible_after_solve=False)
    tracker = ServiceRateTracker()

    result = asyncio.run(
        captcha_solver.solve_captcha_and_view(page, rate_tracker=tracker)
    )

    assert result is False
    assert tracker.totals()["2captcha"] == {"success": 0, "total": 1}


def test_captcha_noop_when_rate_tracker_is_none(monkeypatch):
    """rate_tracker=None → no exception, behavior identical to pre-Phase-2."""
    import config as cfg
    import captcha_solver

    monkeypatch.setattr(cfg, "CAPTCHA_API_KEY", "fake-key", raising=False)
    fake_solver = MagicMock()
    fake_solver.recaptcha.return_value = {"code": "TOKEN_ABC"}
    monkeypatch.setattr(captcha_solver, "TwoCaptcha", lambda key: fake_solver)

    page = _build_mock_captcha_page(content_visible_after_solve=True)

    # No tracker argument at all (legacy signature)
    result_legacy = asyncio.run(captcha_solver.solve_captcha_and_view(page))
    assert result_legacy is True

    # Explicit None
    page2 = _build_mock_captcha_page(content_visible_after_solve=True)
    result_explicit = asyncio.run(
        captcha_solver.solve_captcha_and_view(page2, rate_tracker=None)
    )
    assert result_explicit is True
