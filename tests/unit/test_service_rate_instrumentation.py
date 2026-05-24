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


# ── Smarty ─────────────────────────────────────────────────────────────


def _make_smarty_candidate(*, delivery_line_1: str, city: str = "HUNTSVILLE",
                           state: str = "AL", zipcode: str = "35801") -> MagicMock:
    """Build a Smarty Candidate mock with the fields standardize_addresses reads."""
    components = SimpleNamespace(
        city_name=city,
        state_abbreviation=state,
        zipcode=zipcode,
        plus4_code="",
    )
    metadata = SimpleNamespace(latitude=None, longitude=None, rdi=None)
    analysis = SimpleNamespace(dpv_match_code=None, vacant=None)
    cand = MagicMock()
    cand.delivery_line_1 = delivery_line_1
    cand.components = components
    cand.metadata = metadata
    cand.analysis = analysis
    return cand


def _make_lookup_mock(input_id: str, candidates: list) -> MagicMock:
    """Build a Lookup-like mock that iteration over the Smarty Batch yields."""
    lookup = MagicMock()
    lookup.input_id = input_id
    lookup.result = candidates
    return lookup


def _patch_smarty_batch(monkeypatch, *, results_by_input_id: dict):
    """Patch standardize_addresses' client + batch iteration to return mock lookups.

    results_by_input_id maps "0", "1", ... → list of Candidate mocks (empty = no match).
    """
    import address_standardizer as az

    class _FakeClient:
        def send_batch(self, batch):
            # Replace batch's iteration with our pre-built lookups
            batch._lookups = [
                _make_lookup_mock(iid, cands)
                for iid, cands in results_by_input_id.items()
            ]

    class _FakeBatch:
        def __init__(self):
            self._lookups = []
            self._added = []

        def add(self, lookup):
            self._added.append(lookup)
            # If client.send_batch hasn't pre-populated, default to the added ones
            # (used so iteration during the post-send loop works).

        def __iter__(self):
            return iter(self._lookups)

    monkeypatch.setattr(az, "_build_client", lambda a, b: _FakeClient())
    monkeypatch.setattr(az, "Batch", _FakeBatch)


def test_smarty_records_one_success_per_resolved_address(monkeypatch):
    """One notice resolves (delivery_line_1 populated), one doesn't → 1/2."""
    from notice_parser import NoticeData
    import address_standardizer as az

    notices = [
        NoticeData(address="123 MAIN ST", city="HUNTSVILLE", state="AL", zip="35801"),
        NoticeData(address="999 GHOST RD", city="HUNTSVILLE", state="AL", zip="35801"),
    ]

    _patch_smarty_batch(monkeypatch, results_by_input_id={
        "0": [_make_smarty_candidate(delivery_line_1="123 MAIN ST")],
        "1": [],  # No candidates → failure
    })

    tracker = ServiceRateTracker()
    az.standardize_addresses(notices, "auth-id", "auth-token", rate_tracker=tracker)

    assert tracker.totals()["smarty"] == {"success": 1, "total": 2}


def test_smarty_records_failure_on_http_error(monkeypatch):
    """Batch send raises → every notice in the batch records a failure."""
    from notice_parser import NoticeData
    from smartystreets_python_sdk import exceptions
    import address_standardizer as az

    notices = [
        NoticeData(address=f"{i} TEST ST", city="HUNTSVILLE", state="AL", zip="35801")
        for i in range(3)
    ]

    class _RaisingClient:
        def send_batch(self, batch):
            raise exceptions.SmartyException("simulated 500")

    class _FakeBatch:
        def __init__(self):
            self._added = []

        def add(self, lookup):
            self._added.append(lookup)

        def __iter__(self):
            return iter([])

    monkeypatch.setattr(az, "_build_client", lambda a, b: _RaisingClient())
    monkeypatch.setattr(az, "Batch", _FakeBatch)

    tracker = ServiceRateTracker()
    az.standardize_addresses(notices, "auth-id", "auth-token", rate_tracker=tracker)

    assert tracker.totals()["smarty"] == {"success": 0, "total": 3}


def test_smarty_zip_assuranceweb_records_success_when_zip_returned(monkeypatch):
    """smarty_zip_for_assuranceweb_address records 1 success when (city, zip) returned."""
    import address_standardizer as az

    # Patch _smarty_lookup_once to return a clean (city, zip)
    monkeypatch.setattr(
        az, "_smarty_lookup_once",
        lambda situs, hint: ("ALBERTVILLE", "35950"),
    )

    tracker = ServiceRateTracker()
    city, zip_ = az.smarty_zip_for_assuranceweb_address(
        "123 MAIN ST", "Albertville AL", rate_tracker=tracker,
    )

    assert (city, zip_) == ("ALBERTVILLE", "35950")
    assert tracker.totals()["smarty"] == {"success": 1, "total": 1}


def test_smarty_zip_assuranceweb_records_failure_when_no_match(monkeypatch):
    """smarty_zip_for_assuranceweb_address records 1 failure when all fallbacks miss."""
    import address_standardizer as az

    # Every Smarty call returns empty
    monkeypatch.setattr(az, "_smarty_lookup_once", lambda situs, hint: ("", ""))

    tracker = ServiceRateTracker()
    city, zip_ = az.smarty_zip_for_assuranceweb_address(
        "999 GHOST RD", "Albertville AL",
        anchor_fallbacks=("Boaz AL", "AL"),
        rate_tracker=tracker,
    )

    assert (city, zip_) == ("", "")
    assert tracker.totals()["smarty"] == {"success": 0, "total": 1}


def test_smarty_noop_when_rate_tracker_is_none(monkeypatch):
    """rate_tracker=None on standardize_addresses → no exception, no tracker mutation."""
    from notice_parser import NoticeData
    import address_standardizer as az

    notices = [
        NoticeData(address="123 MAIN ST", city="HUNTSVILLE", state="AL", zip="35801"),
        NoticeData(address="456 OAK AVE", city="HUNTSVILLE", state="AL", zip="35801"),
    ]

    _patch_smarty_batch(monkeypatch, results_by_input_id={
        "0": [_make_smarty_candidate(delivery_line_1="123 MAIN ST")],
        "1": [_make_smarty_candidate(delivery_line_1="456 OAK AVE")],
    })

    # No tracker arg at all
    az.standardize_addresses(notices, "auth-id", "auth-token")

    # Explicit None
    tracker = ServiceRateTracker()
    az.standardize_addresses(
        notices, "auth-id", "auth-token", rate_tracker=None,
    )
    assert tracker.totals()["smarty"] == {"success": 0, "total": 0}


# ── LLM ────────────────────────────────────────────────────────────────


class _FakeAnthropicMessage:
    """Stand-in for anthropic SDK's response.content[0].text shape."""
    def __init__(self, text: str):
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text: str):
        self.content = [_FakeAnthropicMessage(text)]


def _patch_anthropic_sync(monkeypatch, *, return_text: str | None = None,
                          raise_exc: Exception | None = None):
    """Patch anthropic.Anthropic so chat_json doesn't issue real HTTP."""
    import anthropic

    class _FakeClient:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            return _FakeAnthropicResponse(return_text or "")

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)


def test_llm_records_success_when_required_keys_present(monkeypatch):
    """JSON parses + all required_keys present → record success."""
    import llm_client

    _patch_anthropic_sync(
        monkeypatch,
        return_text='{"decedent_full_name": "Jane Doe", "is_obituary": true}',
    )

    tracker = ServiceRateTracker()
    result = llm_client.chat_json(
        prompt="...", api_key="fake",
        rate_tracker=tracker,
        required_keys=("decedent_full_name", "is_obituary"),
    )

    assert result == {"decedent_full_name": "Jane Doe", "is_obituary": True}
    assert tracker.totals()["llm"] == {"success": 1, "total": 1}


def test_llm_records_failure_when_required_keys_missing(monkeypatch):
    """JSON parses but a required key is missing → record failure, return None."""
    import llm_client

    _patch_anthropic_sync(
        monkeypatch,
        return_text='{"some_other_key": "value"}',
    )

    tracker = ServiceRateTracker()
    result = llm_client.chat_json(
        prompt="...", api_key="fake",
        rate_tracker=tracker,
        required_keys=("decedent_full_name",),
    )

    assert result is None
    assert tracker.totals()["llm"] == {"success": 0, "total": 1}


def test_llm_records_failure_on_json_parse_error(monkeypatch):
    """Response isn't valid JSON → record failure, return None."""
    import llm_client

    _patch_anthropic_sync(
        monkeypatch,
        return_text="this is not JSON at all, no braces",
    )

    tracker = ServiceRateTracker()
    result = llm_client.chat_json(
        prompt="...", api_key="fake", rate_tracker=tracker,
    )

    assert result is None
    assert tracker.totals()["llm"] == {"success": 0, "total": 1}


def test_llm_records_failure_on_http_error(monkeypatch):
    """SDK raises → record failure, return None."""
    import llm_client

    _patch_anthropic_sync(
        monkeypatch, raise_exc=RuntimeError("simulated 500"),
    )

    tracker = ServiceRateTracker()
    result = llm_client.chat_json(
        prompt="...", api_key="fake", rate_tracker=tracker,
    )

    assert result is None
    assert tracker.totals()["llm"] == {"success": 0, "total": 1}


def test_llm_extract_with_llm_uses_probate_required_keys(monkeypatch):
    """extract_with_llm(notice_type='probate') passes _PROBATE_KEYS sorted-tuple."""
    import llm_parser

    captured: dict = {}

    async def fake_chat_json_async(prompt, system="", max_tokens=1024, api_key=None,
                                    rate_tracker=None, required_keys=None):
        captured["required_keys"] = required_keys
        captured["rate_tracker"] = rate_tracker
        # Return a fully-populated probate payload so extract_with_llm
        # doesn't reject it (downstream isn't under test here).
        return {
            k: "" for k in llm_parser._PROBATE_KEYS
        } | {
            "decedent_name": "JANE DOE",
            "owner_name": "JOHN DOE",
            "owner_street": "",
            "owner_city": "",
            "owner_state": "AL",
            "owner_zip": "",
            "address": "",
            "city": "",
            "state": "AL",
            "zip": "",
            "case_number": "PC2026-001",
            "judge_name": "Smith",
            "granted_date": "2026-01-01",
        }

    monkeypatch.setattr(llm_parser.llm_client, "chat_json_async", fake_chat_json_async)

    tracker = ServiceRateTracker()
    asyncio.run(llm_parser.extract_with_llm(
        raw_text="some probate notice text",
        notice_type="probate",
        county="Jefferson",
        api_key="fake",
        rate_tracker=tracker,
    ))

    rk = captured.get("required_keys")
    assert rk is not None, "required_keys must be passed to chat_json_async"
    assert isinstance(rk, tuple), f"required_keys must be a tuple (got {type(rk).__name__})"
    # Probate keys must include the structural anchors and EXCLUDE attorney_name
    assert "decedent_name" in rk
    assert "case_number" in rk
    assert "attorney_name" not in rk, "attorney_name is intentionally optional"
    # tracker plumbed through
    assert captured["rate_tracker"] is tracker


def test_llm_noop_when_rate_tracker_is_none(monkeypatch):
    """rate_tracker=None on chat_json → no exception, behavior unchanged."""
    import llm_client

    _patch_anthropic_sync(
        monkeypatch,
        return_text='{"x": 1}',
    )

    # No rate_tracker kwarg at all
    result_legacy = llm_client.chat_json(prompt="...", api_key="fake")
    assert result_legacy == {"x": 1}

    # Explicit None — tracker untouched
    tracker = ServiceRateTracker()
    result_explicit = llm_client.chat_json(
        prompt="...", api_key="fake", rate_tracker=None,
    )
    assert result_explicit == {"x": 1}
    assert tracker.totals()["llm"] == {"success": 0, "total": 0}
