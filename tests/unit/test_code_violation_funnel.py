"""code_violation pipeline funnel + slack-blocks wiring tests (Phase 2, OPS-03 / OBS-01).

Drives ``code_violation_pipeline.fetch_code_violations`` through a
fully-mocked synthetic run (no real Huntsville PDF fetch, no real
Birmingham Accela / Playwright, no real Hoover SeeClickFix HTTP, no real
Smarty, no real Slack) and asserts:

  1. All 3 D-01 gates populate with the expected counts
  2. The Slack blocks payload contains the funnel block + service-rates
     block (D-02 — one message, three blocks)
  3. save_rolling_rates fires exactly ONCE, AFTER _send_blocks_webhook
     returns True (rolling-rates ordering — D-03 / plan-checker W6)

All file I/O (observability.STATE_FILE) routes through tmp_path so the
real ``output/observability/`` directory is NEVER touched during tests.
"""

from __future__ import annotations

import pytest

import code_violation_pipeline as pipeline
import observability
from notice_parser import NoticeData


# ── Synthetic NoticeData builders (mirror what the adapters produce) ──


def _make_huntsville_notice(idx: int, *, with_owner: bool, in_tier: bool) -> NoticeData:
    n = NoticeData()
    n.county = "Madison"
    n.state = "AL"
    n.notice_type = "code_violation"
    n.notice_subtype = "unsafe_building"
    n.municipality = "Huntsville"
    n.address = f"{300 + idx} Huntsville Blvd"
    n.city = "Huntsville"
    # 35810 is Tier 1 Madison; 35999 is off-tier.
    n.zip = "35810" if in_tier else "35999"
    n.case_number = f"CE-26-{idx:04d}"
    n.owner_name = f"Owner H{idx}" if with_owner else ""
    n.date_added = "2026-05-24"
    return n


def _make_birmingham_notice(idx: int, *, with_owner: bool, in_tier: bool) -> NoticeData:
    n = NoticeData()
    n.county = "Jefferson"
    n.state = "AL"
    n.notice_type = "code_violation"
    n.notice_subtype = "housing_enforcement"
    n.municipality = "Birmingham"
    n.address = f"{400 + idx} Bham Ave"
    n.city = "Birmingham"
    # 35023 = Tier 1 Hueytown/Bessemer area; 35999 off-tier.
    n.zip = "35023" if in_tier else "35999"
    n.case_number = f"HEN2026-{idx:04d}"
    n.owner_name = f"Owner B{idx}" if with_owner else ""
    n.date_added = "2026-05-24"
    return n


def _make_hoover_notice(idx: int, *, with_owner: bool) -> NoticeData:
    n = NoticeData()
    n.county = "Jefferson"
    n.state = "AL"
    n.notice_type = "code_violation"
    n.notice_subtype = "code_enforcement_complaint"
    n.municipality = "Hoover"
    n.address = f"{500 + idx} Hoover Rd"
    n.city = "Hoover"
    # 35226 = Tier 1 Hoover; always in-tier (Hoover adapter pre-filters).
    n.zip = "35226"
    n.owner_name = f"Owner V{idx}" if with_owner else ""
    n.date_added = "2026-05-24"
    return n


# ── Fixture: pre-wire all the mocks every test needs ────────────────


@pytest.fixture
def mocked_pipeline(monkeypatch, tmp_path):
    """Pre-wire monkeypatches every test needs.

    Scenario (with ``enrich_owner=True``):
      - Huntsville: 4 notices — 3 with owner_name (Smarty/Madison hit),
        1 without. 3 in-tier (35810), 1 off-tier (35999).
      - Birmingham: 3 notices — 2 with owner_name, 1 without. 2 in-tier
        (35023), 1 off-tier (35999).
      - Hoover: 2 notices — both with owner_name, both in-tier (35226).
      - Total bulk_fetched = 9
      - owner_enriched = 7 (3 + 2 + 2)
      - tier_gated = 7 (3 + 2 + 2 — the 2 off-tier records drop here)
    """
    state_file = tmp_path / "service_rates.json"
    monkeypatch.setattr(observability, "STATE_FILE", state_file)

    huntsville_notices = [
        _make_huntsville_notice(0, with_owner=True, in_tier=True),
        _make_huntsville_notice(1, with_owner=True, in_tier=True),
        _make_huntsville_notice(2, with_owner=True, in_tier=True),
        _make_huntsville_notice(3, with_owner=False, in_tier=False),
    ]
    birmingham_notices = [
        _make_birmingham_notice(0, with_owner=True, in_tier=True),
        _make_birmingham_notice(1, with_owner=True, in_tier=True),
        _make_birmingham_notice(2, with_owner=False, in_tier=False),
    ]
    hoover_notices = [
        _make_hoover_notice(0, with_owner=True),
        _make_hoover_notice(1, with_owner=True),
    ]

    monkeypatch.setattr(
        pipeline, "_fetch_madison",
        lambda *, min_age_years, enrich_owner: list(huntsville_notices),
    )
    monkeypatch.setattr(
        pipeline, "_fetch_jefferson",
        lambda **kw: list(birmingham_notices),
    )
    monkeypatch.setattr(
        pipeline, "_fetch_hoover",
        lambda *, days_back, enrich_owner, target_zips_only:
            list(hoover_notices),
    )

    return {
        "state_file": state_file,
        "tmp_path": tmp_path,
        "huntsville_notices": huntsville_notices,
        "birmingham_notices": birmingham_notices,
        "hoover_notices": hoover_notices,
    }


# ── Test 1: all 3 D-01 gates populate ─────────────────────────────────


def test_code_violation_funnel_records_all_gates(mocked_pipeline):
    """Drive a fully-mocked run; assert all 3 D-01 gates populated."""
    notices, funnel, rate_tracker = pipeline.fetch_code_violations(
        counties=("Madison", "Jefferson"),
        enrich_owner=True,
        tiers=(1, 2),
    )

    gates = funnel.as_ordered_dict()

    # D-01 invariant: pre-seeded gate order matches the canonical sequence.
    assert list(gates.keys()) == [
        "bulk_fetched", "owner_enriched", "tier_gated",
    ]

    # bulk_fetched: 4 Huntsville + 3 Birmingham + 2 Hoover = 9
    assert gates["bulk_fetched"] == 9
    # owner_enriched: 3 + 2 + 2 = 7 notices with owner_name populated
    assert gates["owner_enriched"] == 7
    # tier_gated: 3 in-tier Huntsville + 2 in-tier Birmingham + 2 Hoover
    # (Hoover always in-tier) = 7
    assert gates["tier_gated"] == 7

    # Verify returned notices match tier_gated count
    assert len(notices) == 7


# ── Test 2: Slack blocks contain funnel + service-rates sections ─────


def test_code_violation_slack_includes_funnel_and_rates_blocks(
    mocked_pipeline, monkeypatch,
):
    """Capture the blocks payload sent to _send_blocks_webhook and assert
    it contains a Funnel section + Service Rates section (D-02)."""
    captured: dict = {}

    def fake_send(text, blocks, webhook_url=None):
        captured["text"] = text
        captured["blocks"] = blocks
        captured["webhook_url"] = webhook_url
        return True

    monkeypatch.setattr(pipeline, "_send_blocks_webhook", fake_send)
    # Defang save_rolling_rates so the real one isn't called (test 3 covers
    # the ordering assertion explicitly).
    monkeypatch.setattr(pipeline, "save_rolling_rates", lambda totals, **kw: {})

    notices, funnel, rate_tracker = pipeline.fetch_code_violations(
        counties=("Madison", "Jefferson"),
        enrich_owner=True,
        tiers=(1, 2),
    )

    # Simulate some Wave 2 service-rate records on the shared tracker —
    # in a real run, the adapter modules' enrich_with_owner paths feed
    # these via the Smarty wrappers (Wave 2 contract). The funnel-wiring
    # test concerns itself with the BLOCK shape, not whether each adapter
    # is correctly recording — those are covered by Wave 2 tests.
    for _ in range(5):
        rate_tracker.record("smarty", True)
    for _ in range(2):
        rate_tracker.record("smarty", False)

    sent = pipeline.notify_slack(
        notices, funnel, rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # Should be 3 blocks: summary section + funnel + rates.
    assert len(captured["blocks"]) >= 3

    # Funnel block contains the pipeline name + all 3 gates.
    funnel_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Funnel — code_violation" in b.get("text", {}).get("text", "")
    )
    assert "bulk_fetched" in funnel_block_text
    assert "owner_enriched" in funnel_block_text
    assert "tier_gated" in funnel_block_text

    # Service-rates block contains all 4 service labels.
    rates_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Service Rates" in b.get("text", {}).get("text", "")
    )
    for label in ("2Captcha", "Smarty", "Tracerfy", "LLM"):
        assert label in rates_block_text

    # Per-run Smarty rate should be 71% today (5/7 hits → round(5/7*100)=71).
    assert "71% today" in rates_block_text

    # Summary header contains the per-county counts.
    assert "Code-Violation Run" in captured["text"]


# ── Test 3: save_rolling_rates fires AFTER _send_blocks_webhook ──────


def test_code_violation_save_rolling_rates_called_after_slack_post(
    mocked_pipeline, monkeypatch,
):
    """Rolling-rates ordering (D-03 / W6): load_rolling_rates BEFORE the
    blocks build, save_rolling_rates AFTER successful send."""
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

    notices, funnel, rate_tracker = pipeline.fetch_code_violations(
        counties=("Madison", "Jefferson"),
        enrich_owner=True,
        tiers=(1, 2),
    )

    # Record a couple of Smarty + LLM calls so the totals propagation is
    # observable in the save_args assertion.
    rate_tracker.record("smarty", True)
    rate_tracker.record("smarty", True)
    rate_tracker.record("smarty", False)
    rate_tracker.record("llm", True)

    sent = pipeline.notify_slack(
        notices, funnel, rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # send MUST precede save — exactly one of each.
    assert call_order == ["send", "save"]

    # save_rolling_rates received the tracker's totals() dict (4 services).
    totals = save_args["totals"]
    assert set(totals.keys()) == {"2captcha", "smarty", "tracerfy", "llm"}
    # Smarty received 3 calls (2 success + 1 failure per the inline
    # records above).
    assert totals["smarty"] == {"success": 2, "total": 3}
    assert totals["llm"] == {"success": 1, "total": 1}
