"""main.py daily-flow funnel + slack-blocks wiring tests (Phase 2, OPS-03 / OBS-01).

Drives the ``_post_daily_slack_with_funnel`` helper (the single Slack
seam that both ``actor_main`` and ``_run_scrape_pipeline`` route their
end-of-run summary through) and asserts:

  1. All 10 D-01 main_daily gates populate with the expected counts
     after a synthetic daily flow stamps each owner's gates
  2. The Slack blocks payload contains the legacy summary section +
     funnel block + service-rates block (D-02 — one message, three
     blocks)
  3. save_rolling_rates fires exactly ONCE, AFTER _send_blocks_webhook
     returns True (rolling-rates ordering — D-03 / W6)

All file I/O (observability.STATE_FILE) routes through tmp_path so the
real ``output/observability/`` directory is NEVER touched during tests.
"""

from __future__ import annotations

import pytest

import main
import observability
from notice_parser import NoticeData
from observability import FunnelCounter, ServiceRateTracker


# ── Test fixtures ─────────────────────────────────────────────────────


def _make_notice(idx: int, owner_deceased: str = "") -> NoticeData:
    """Build a minimal NoticeData suitable for build_summary + funnel stamps."""
    n = NoticeData()
    n.notice_type = "foreclosure"
    n.county = "Jefferson"
    n.address = f"{100 + idx} Main St"
    n.city = "Birmingham"
    n.state = "AL"
    n.zip = "35023"
    n.owner_name = f"Owner {idx:02d}"
    n.owner_deceased = owner_deceased
    n.date_added = "2026-05-24"
    return n


def _drive_synthetic_daily_funnel(
    funnel: FunnelCounter,
    rate_tracker: ServiceRateTracker,
    *,
    scraped: int = 10,
    parcel_matched: int = 8,
    smarty_matched: int = 7,
    zillow_matched: int = 6,
    tracerfy_matched: int = 4,
    final_count: int = 6,
) -> None:
    """Drive the 10 gates the way main.py + full_pipeline + enrichment_pipeline
    would in a real run — main.py owns scraped + seen_ids_deduped +
    datasift_uploaded; enrichment_pipeline owns the 6 middle gates;
    full_pipeline owns tracerfy_matched.
    """
    # main.py-owned gates (start)
    funnel.set("scraped", scraped)
    funnel.set("seen_ids_deduped", scraped)

    # enrichment_pipeline-owned gates
    funnel.set("county_filtered", scraped)
    funnel.set("parsed", scraped)
    funnel.set("tier_gated", scraped)
    funnel.set("al_property_enriched", parcel_matched)
    funnel.set("smarty_standardized", smarty_matched)
    funnel.set("zillow_enriched", zillow_matched)

    # full_pipeline-owned gate (Tracerfy)
    funnel.set("tracerfy_matched", tracerfy_matched)

    # Also record into the rate tracker so per_run_rates() returns real
    # numbers — mirrors the Wave 2 service-rate instrumentation that
    # would fire inside the real Smarty / LLM / Tracerfy / Captcha
    # call sites.
    for _ in range(scraped):
        rate_tracker.record("2captcha", True)
    for _ in range(smarty_matched):
        rate_tracker.record("smarty", True)
    for _ in range(scraped - smarty_matched):
        rate_tracker.record("smarty", False)
    for _ in range(tracerfy_matched):
        rate_tracker.record("tracerfy", True)
    for _ in range(parcel_matched - tracerfy_matched):
        rate_tracker.record("tracerfy", False)
    for _ in range(scraped):
        rate_tracker.record("llm", True)

    # main.py-owned gate (end)
    funnel.set("datasift_uploaded", final_count)


@pytest.fixture
def funnel_and_tracker(monkeypatch, tmp_path):
    """Build a fresh main_daily FunnelCounter + ServiceRateTracker.

    Routes observability.STATE_FILE to tmp_path so save_rolling_rates
    cannot pollute the real output/observability/service_rates.json
    during the test.
    """
    state_file = tmp_path / "service_rates.json"
    monkeypatch.setattr(observability, "STATE_FILE", state_file)

    funnel = FunnelCounter("main_daily", gates=list(main.MAIN_DAILY_GATES))
    rate_tracker = ServiceRateTracker()
    return funnel, rate_tracker, state_file


# ── Test 1: All 10 gates populated ────────────────────────────────────


def test_main_daily_funnel_records_all_gates(funnel_and_tracker):
    """Drive a synthetic daily flow; assert all 10 D-01 gates populated
    in the canonical order with expected counts."""
    funnel, rate_tracker, _ = funnel_and_tracker

    _drive_synthetic_daily_funnel(
        funnel, rate_tracker,
        scraped=10, parcel_matched=8, smarty_matched=7,
        zillow_matched=6, tracerfy_matched=4, final_count=6,
    )

    gates = funnel.as_ordered_dict()

    # Insertion-order assertion: MAIN_DAILY_GATES is the canonical
    # sequence; the OrderedDict keys must match it 1:1.
    assert list(gates.keys()) == list(main.MAIN_DAILY_GATES)

    # Per-gate counts.
    assert gates["scraped"] == 10
    assert gates["seen_ids_deduped"] == 10
    assert gates["county_filtered"] == 10
    assert gates["parsed"] == 10
    assert gates["tier_gated"] == 10
    assert gates["al_property_enriched"] == 8
    assert gates["smarty_standardized"] == 7
    assert gates["zillow_enriched"] == 6
    assert gates["tracerfy_matched"] == 4
    assert gates["datasift_uploaded"] == 6


# ── Test 2: Slack blocks contain funnel + service-rates sections ─────


def test_main_daily_slack_includes_funnel_and_rates_blocks(
    funnel_and_tracker, monkeypatch,
):
    """Capture the blocks payload _post_daily_slack_with_funnel sends and
    assert it contains a Funnel section + Service Rates section (D-02)."""
    funnel, rate_tracker, _ = funnel_and_tracker
    _drive_synthetic_daily_funnel(funnel, rate_tracker)

    captured: dict = {}

    def fake_send(text, blocks, webhook_url=None):
        captured["text"] = text
        captured["blocks"] = blocks
        captured["webhook_url"] = webhook_url
        return True  # simulate 200 response

    monkeypatch.setattr(main, "_send_blocks_webhook", fake_send)
    # Defang save_rolling_rates so it doesn't fire here — Test 3 checks
    # ordering specifically.
    monkeypatch.setattr(main, "save_rolling_rates", lambda totals, **kw: {})

    notices = [_make_notice(i) for i in range(6)]
    sent = main._post_daily_slack_with_funnel(
        notices,
        funnel,
        rate_tracker,
        elapsed_min=12.5,
        cost_breakdown={"2Captcha": 0.03, "Tracerfy": 0.08},
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # Should be 3 blocks: legacy summary section + funnel + rates.
    assert len(captured["blocks"]) == 3

    # Block 1: summary section (legacy build_summary output).
    summary_text = captured["blocks"][0]["text"]["text"]
    assert "SiftStack" in summary_text
    assert "New notices scraped" in summary_text

    # Block 2: funnel block contains the pipeline name + the 10 gates.
    funnel_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Funnel — main_daily" in b.get("text", {}).get("text", "")
    )
    for gate in main.MAIN_DAILY_GATES:
        assert gate in funnel_block_text, f"funnel block missing gate {gate!r}"

    # Block 3: service-rates block contains all 4 service labels.
    rates_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Service Rates" in b.get("text", {}).get("text", "")
    )
    for label in ("2Captcha", "Smarty", "Tracerfy", "LLM"):
        assert label in rates_block_text


# ── Test 3: save_rolling_rates fires AFTER _send_blocks_webhook ──────


def test_main_daily_save_rolling_rates_called_after_slack_post(
    funnel_and_tracker, monkeypatch,
):
    """Rolling-rates ordering (D-03 / W6):
    load_rolling_rates BEFORE the blocks build, save_rolling_rates AFTER
    a successful Slack post. Asserts the send → save sequence via call-
    order capture, and confirms the tracker's totals() reach save."""
    funnel, rate_tracker, _ = funnel_and_tracker
    _drive_synthetic_daily_funnel(funnel, rate_tracker)

    call_order: list[str] = []
    save_args: dict = {}

    def fake_send(text, blocks, webhook_url=None):
        call_order.append("send")
        return True

    def fake_save(totals, **kw):
        call_order.append("save")
        save_args["totals"] = totals
        save_args["kw"] = kw
        return {}

    monkeypatch.setattr(main, "_send_blocks_webhook", fake_send)
    monkeypatch.setattr(main, "save_rolling_rates", fake_save)

    notices = [_make_notice(i) for i in range(6)]
    sent = main._post_daily_slack_with_funnel(
        notices,
        funnel,
        rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # send MUST precede save (one of each, exactly once).
    assert call_order == ["send", "save"]

    # save_rolling_rates received the tracker's totals() dict (4 services).
    totals = save_args["totals"]
    assert set(totals.keys()) == {"2captcha", "smarty", "tracerfy", "llm"}
    # Tracerfy received 8 calls in the synthetic flow: 4 matched +
    # (parcel_matched=8 - tracerfy_matched=4)=4 failures.
    assert totals["tracerfy"] == {"success": 4, "total": 8}
    # 2Captcha received 10 successes from the synthetic flow (one per
    # scraped notice).
    assert totals["2captcha"] == {"success": 10, "total": 10}


# ── Test 4: failed Slack post does NOT advance the rolling baseline ──


def test_main_daily_save_skipped_when_slack_post_fails(
    funnel_and_tracker, monkeypatch,
):
    """W6: a failed _send_blocks_webhook must leave the rolling baseline
    untouched — save_rolling_rates is guarded behind ``if sent``."""
    funnel, rate_tracker, _ = funnel_and_tracker
    _drive_synthetic_daily_funnel(funnel, rate_tracker)

    save_called: list[bool] = []

    monkeypatch.setattr(
        main, "_send_blocks_webhook",
        lambda text, blocks, webhook_url=None: False,
    )
    monkeypatch.setattr(
        main, "save_rolling_rates",
        lambda totals, **kw: save_called.append(True),
    )

    sent = main._post_daily_slack_with_funnel(
        [_make_notice(0)],
        funnel,
        rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is False
    assert save_called == []
