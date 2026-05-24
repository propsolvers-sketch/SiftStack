"""apn_probate pipeline funnel + slack-blocks wiring tests (Phase 2, OPS-03 / OBS-01).

Drives apn_probate_pipeline_al through a fully-mocked simulated run (no
real APN scrape, no real probate property API, no real Tracerfy, no real
Slack) and asserts:

  1. All 6 D-01 gates populate with the expected counts
  2. The Slack blocks payload contains the funnel block + service-rates
     block (D-02 — one message, three blocks)
  3. save_rolling_rates fires exactly ONCE, AFTER _send_blocks_webhook
     returns True (rolling-rates ordering — D-03 / plan-checker W6)

All file I/O (observability.STATE_FILE) routes through tmp_path so the
real ``output/observability/`` directory is NEVER touched during tests.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path

import pytest

import apn_probate_pipeline_al as pipeline
import observability
from notice_parser import NoticeData
from observability import FunnelCounter, ServiceRateTracker


# ── Test fixtures ─────────────────────────────────────────────────────


def _make_probate_notice(
    case_number: str,
    decedent_name: str,
    county: str,
    granted_date: str = "2026-01-15",
) -> NoticeData:
    """Build a minimal probate NoticeData suitable for pipeline-stage mocks."""
    n = NoticeData()
    n.notice_type = "probate"
    n.case_number = case_number
    n.decedent_name = decedent_name
    n.county = county
    n.granted_date = granted_date
    n.raw_text = f"Estate of {decedent_name}, deceased"
    return n


@pytest.fixture
def mocked_pipeline(monkeypatch, tmp_path):
    """Pre-wire monkeypatches that every test in this file needs.

    - observability.STATE_FILE → tmp_path so no real file written
    - cfg.SAVED_SEARCHES → a single Jefferson probate search so
      _filter_probate_searches finds something
    - scrape_all → returns 10 synthetic probate notices
    - enrich_notice_with_property → marks the first 8 with parcel_id +
      address + zip (last 2 get nothing → drop at no_property)
    - tier_filter → 6 of the 8 land in Tier 1/2 ZIPs (2 off-tier)
    - batch_skip_trace → returns {submitted: 6, matched: 4, ...}
    - datasift_formatter.write_datasift_csv → no-op
    """
    state_file = tmp_path / "service_rates.json"
    monkeypatch.setattr(observability, "STATE_FILE", state_file)

    # Build 10 synthetic notices, alternating Jefferson + Madison.
    # _normalize_decedent_key strips non-alpha (so digits in names
    # collapse) AND uses (first-3-chars + last-name + DoD) as the dedup
    # key — so we need 10 alpha-only, unique-last-name decedents OR else
    # the same-person dedup at run_pipeline line ~178 will collapse them.
    _last_names = [
        "Alpha", "Bravo", "Charlie", "Delta", "Echo",
        "Foxtrot", "Golf", "Hotel", "India", "Juliet",
    ]
    notices = [
        _make_probate_notice(
            f"PR-2026-{i:04d}",
            f"Joseph {_last_names[i]}",
            "Jefferson" if i % 2 == 0 else "Madison",
        )
        for i in range(10)
    ]

    # Fake SAVED_SEARCH so _filter_probate_searches returns something
    class _FakeSearch:
        notice_type = "probate"
        county = "Jefferson"

    monkeypatch.setattr(pipeline.cfg, "SAVED_SEARCHES", [_FakeSearch()])

    async def fake_scrape_all(**kwargs):
        return notices

    monkeypatch.setattr(pipeline, "scrape_all", fake_scrape_all)

    # Property locator: first 8 get a parcel + Tier-1 ZIP (35023 Hueytown,
    # 35810 Huntsville). The first 6 of those land in target tier ZIPs
    # (T1/T2); positions 6 + 7 get an off-tier ZIP so tier_gated drops to
    # 6. Positions 8 + 9 get NO property → drop at no_property.
    in_tier_zips = ["35023", "35810", "35023", "35810", "35023", "35810"]
    off_tier_zips = ["35999", "35888"]  # not in any tier

    def fake_enrich(n: NoticeData) -> bool:
        idx = int(n.case_number.split("-")[-1])
        if idx < 6:
            n.parcel_id = f"PARCEL-{idx:04d}"
            n.address = f"{100 + idx} Main St"
            n.city = "Birmingham" if n.county == "Jefferson" else "Huntsville"
            n.zip = in_tier_zips[idx]
            return True
        if idx < 8:
            n.parcel_id = f"PARCEL-{idx:04d}"
            n.address = f"{100 + idx} Main St"
            n.city = "Birmingham"
            n.zip = off_tier_zips[idx - 6]
            return True
        # idx 8 + 9 → no property
        return False

    monkeypatch.setattr(pipeline, "enrich_notice_with_property", fake_enrich)

    # zip_tier_county already returns the correct tier for 35023/35810
    # (both Tier 1 per target_zips.py). off-tier ZIPs return (None, None).

    # Smarty fallback should NOT fire for our test ZIPs (they're already
    # populated above) but defensively stub it just in case.
    monkeypatch.setattr(
        pipeline, "smarty_zip_for_madison_address",
        lambda *a, **kw: ("", ""),
    )
    monkeypatch.setattr(
        pipeline, "smarty_zip_for_marshall_address",
        lambda *a, **kw: ("", ""),
    )

    # Tracerfy: 6 submitted, 4 matched.
    import tracerfy_skip_tracer

    def fake_batch_skip_trace(notices, **kw):
        # Honour the rate_tracker contract (Wave 2 — record outcomes).
        tracker = kw.get("rate_tracker")
        if tracker is not None:
            for _ in range(4):
                tracker.record("tracerfy", True)
            for _ in range(2):
                tracker.record("tracerfy", False)
        return {
            "submitted": 6, "matched": 4, "phones_found": 8,
            "emails_found": 3, "cost": 0.12,
        }

    monkeypatch.setattr(
        tracerfy_skip_tracer, "batch_skip_trace", fake_batch_skip_trace,
    )

    # No-op the heir-promotion helper (it touches NoticeData internals
    # not relevant to the funnel assertion).
    monkeypatch.setattr(
        pipeline, "_promote_heir_contacts_to_csv_slots", lambda n: None,
    )

    # CSV writer: no-op, return a fake path.
    import datasift_formatter
    monkeypatch.setattr(
        datasift_formatter, "write_datasift_csv",
        lambda notices, **kw: tmp_path / "fake.csv",
    )

    return {
        "notices": notices,
        "state_file": state_file,
        "tmp_path": tmp_path,
    }


# ── Test 1: All 6 gates populated ─────────────────────────────────────


def test_apn_probate_funnel_records_all_gates(mocked_pipeline):
    """Drive a fully-mocked run; assert all 6 D-01 gates populated."""
    results, funnel, rate_tracker = asyncio.run(pipeline.run_pipeline(
        counties=("Jefferson",),
        days_back=7,
        tier_filter=(1, 2),
        max_notices=20,
    ))

    # Run prepare_notices → exercises the Tracerfy + tracerfy_matched gate.
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )

    # Stamp the datasift_uploaded gate (this happens in _cli after CSV
    # write — replicate that here for the assertion).
    funnel.set("datasift_uploaded", len(notices))

    gates = funnel.as_ordered_dict()

    # Insertion-order assertion: pipeline.run_pipeline pre-seeds the
    # full 6-gate sequence so OrderedDict keys match D-01.
    assert list(gates.keys()) == [
        "scraped", "seen_ids_deduped", "decedent_name_searched",
        "tier_gated", "tracerfy_matched", "datasift_uploaded",
    ]

    # scraped + seen_ids_deduped: scrape_all returned 10 (scraper handles
    # dedup internally — both gates show the same value per the plan).
    assert gates["scraped"] == 10
    assert gates["seen_ids_deduped"] == 10
    # decedent_name_searched: 8 notices got a parcel_id (locator matched).
    assert gates["decedent_name_searched"] == 8
    # tier_gated: 6 survived the Tier-1/2 filter (positions 0-5).
    assert gates["tier_gated"] == 6
    # tracerfy_matched: 4 of 6 submitted matched.
    assert gates["tracerfy_matched"] == 4
    # datasift_uploaded: all 6 enriched notices written to CSV.
    assert gates["datasift_uploaded"] == 6


# ── Test 2: Slack blocks contain funnel + service-rates sections ─────


def test_apn_probate_slack_includes_funnel_and_rates_blocks(
    mocked_pipeline, monkeypatch,
):
    """Capture the blocks payload sent to _send_blocks_webhook and assert
    it contains a Funnel section + Service Rates section (D-02)."""
    captured: dict = {}

    def fake_send(text, blocks, webhook_url=None):
        captured["text"] = text
        captured["blocks"] = blocks
        captured["webhook_url"] = webhook_url
        return True  # simulate 200 response

    monkeypatch.setattr(pipeline, "_send_blocks_webhook", fake_send)
    # Defang save_rolling_rates so the real one isn't called with a
    # mocked send-success (we only care about block content here).
    monkeypatch.setattr(pipeline, "save_rolling_rates", lambda totals, **kw: {})

    results, funnel, rate_tracker = asyncio.run(pipeline.run_pipeline(
        counties=("Jefferson",),
        days_back=7,
        tier_filter=(1, 2),
        max_notices=20,
    ))
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )
    funnel.set("datasift_uploaded", len(notices))

    sent = pipeline.notify_slack(
        results,
        skip_trace_stats=stats,
        funnel=funnel,
        rate_tracker=rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # Should be 3 blocks: existing-summary section + funnel + rates.
    assert len(captured["blocks"]) >= 3

    # Funnel block contains the pipeline name + at least one gate.
    funnel_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Funnel — apn_probate" in b.get("text", {}).get("text", "")
    )
    assert "scraped" in funnel_block_text
    assert "tracerfy_matched" in funnel_block_text

    # Service-rates block contains all 4 service labels.
    rates_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Service Rates" in b.get("text", {}).get("text", "")
    )
    for label in ("2Captcha", "Smarty", "Tracerfy", "LLM"):
        assert label in rates_block_text


# ── Test 3: save_rolling_rates fires AFTER _send_blocks_webhook ──────


def test_apn_probate_save_rolling_rates_called_after_slack_post(
    mocked_pipeline, monkeypatch,
):
    """Rolling-rates ordering (CONTEXT.md D-03 / plan-checker W6):
    load_rolling_rates BEFORE the blocks build, save_rolling_rates AFTER
    a successful Slack post. This test asserts the send → save sequence
    via call-order capture."""
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

    monkeypatch.setattr(pipeline, "_send_blocks_webhook", fake_send)
    monkeypatch.setattr(pipeline, "save_rolling_rates", fake_save)

    results, funnel, rate_tracker = asyncio.run(pipeline.run_pipeline(
        counties=("Jefferson",),
        days_back=7,
        tier_filter=(1, 2),
        max_notices=20,
    ))
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )
    funnel.set("datasift_uploaded", len(notices))

    sent = pipeline.notify_slack(
        results,
        skip_trace_stats=stats,
        funnel=funnel,
        rate_tracker=rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # send MUST precede save (one of each, exactly once).
    assert call_order == ["send", "save"]

    # save_rolling_rates received the tracker's totals() dict (4 services).
    totals = save_args["totals"]
    assert set(totals.keys()) == {"2captcha", "smarty", "tracerfy", "llm"}
    # tracerfy received 6 calls (4 success + 2 failure per mocked
    # batch_skip_trace fixture above).
    assert totals["tracerfy"] == {"success": 4, "total": 6}
