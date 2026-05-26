"""tax_distress pipeline funnel + slack-blocks wiring tests (Phase 2, OPS-03 / OBS-01).

Drives ``tax_distress_pipeline.fetch_tax_distress`` through a fully-mocked
synthetic run (no real Madison/Jefferson/Marshall HTTP, no real Smarty,
no real Slack) and asserts:

  1. All 5 D-01 gates populate with the expected counts
  2. The Slack blocks payload contains the funnel block + service-rates
     block (D-02 — one message, three blocks)
  3. save_rolling_rates fires exactly ONCE, AFTER _send_blocks_webhook
     returns True (rolling-rates ordering — D-03 / plan-checker W6)

All file I/O (observability.STATE_FILE) routes through tmp_path so the
real ``output/observability/`` directory is NEVER touched during tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import observability
import tax_distress_pipeline as pipeline


# ── Synthetic raw-record fakes (mirror the adapter dataclass surfaces) ──


@dataclass
class _FakeMadisonRec:
    """Minimal stand-in for madison_tax_delinquent_api.MadisonDelinquentRecord."""
    parcel_id: str
    owner_name: str
    situs_address: str
    balance_due: float
    is_individual_owner: bool
    is_tax_sale_parcel: bool = False
    pcli_id: str = "pcli-x"
    account: str = "acct-x"
    tax_year: int = 2025
    legal_description: str = "Lot 1"
    assessed_value: float = 100000.0


@dataclass
class _FakeJeffersonRec:
    """Minimal stand-in for jefferson_tax_delinquent_api.JeffersonDelinquentRecord."""
    parcel_id: str
    owner_name: str
    situs_address: str
    situs_city: str
    situs_state: str
    situs_zip: str
    balance_due: float
    is_individual_owner: bool
    district: str = "Birmingham"
    lien_num: str = "1"
    mailing_address: str = ""
    mailing_city: str = ""
    mailing_state: str = ""
    mailing_zip: str = ""
    situs_raw: str = ""
    land_value: float = 0.0
    building_value: float = 0.0
    final_value: float = 0.0
    assessed_value: float = 100000.0
    tax_year: int = 2024
    redemption_amount: float = 0.0
    redemption_years: int = 0
    legal_description: str = "Lot 1"
    is_high_exposure: bool = True


# ── Adapter to_notice_data stubs (don't import real adapters; mock them) ─


def _fake_madison_to_notice_data(rec):
    from notice_parser import NoticeData
    n = NoticeData()
    n.county = "Madison"
    n.state = "AL"
    n.notice_type = "tax_sale" if rec.is_tax_sale_parcel else "tax_delinquent"
    n.owner_name = rec.owner_name
    n.tax_owner_name = rec.owner_name
    n.address = rec.situs_address
    n.parcel_id = rec.parcel_id
    n.tax_delinquent_amount = f"{rec.balance_due:.2f}"
    n.assessed_value = f"{rec.assessed_value:.0f}"
    # Madison bulk feed lacks ZIP — leave empty (Smarty fills it).
    return n


def _fake_jefferson_to_notice_data(rec):
    from notice_parser import NoticeData
    n = NoticeData()
    n.county = "Jefferson"
    n.state = "AL"
    n.notice_type = "tax_sale"
    n.owner_name = rec.owner_name
    n.tax_owner_name = rec.owner_name
    n.address = rec.situs_address
    n.city = rec.situs_city
    n.zip = rec.situs_zip  # Jefferson roster includes ZIP
    n.parcel_id = rec.parcel_id
    n.tax_delinquent_amount = f"{rec.balance_due:.2f}"
    n.assessed_value = f"{rec.assessed_value:.0f}"
    return n


# ── Fixture: pre-wire all the mocks every test needs ────────────────


@pytest.fixture
def mocked_pipeline(monkeypatch, tmp_path):
    """Pre-wire monkeypatches every test needs.

    Scenario:
      - 6 Madison records (4 individuals @ $7K, 1 entity @ $9K, 1 individual @ $3K)
      - 4 Jefferson records (3 individuals @ $8K, 1 individual @ $2K)
      - Total raw = 10
      - After individuals_only=True: 6 + 3 = 9 (drop entity)
      - After min_balance >= $5K: 4 + 3 = 7 (drop low-balance survivors)
      - Smarty geocodes 3 of 4 Madison survivors to in-tier ZIPs;
        1 Madison Smarty miss has zip="" → drops at tier_gated.
        All 3 Jefferson survivors already have in-tier ZIPs.
      - smarty_geocoded count = 6 (3 Madison Smarty hits + 3 Jefferson always-with-ZIP)
      - tier_gated = 6 (3 Madison + 3 Jefferson — Smarty miss had no ZIP so dropped)
    """
    state_file = tmp_path / "service_rates.json"
    monkeypatch.setattr(observability, "STATE_FILE", state_file)

    madison_recs = [
        _FakeMadisonRec(f"M-{i:02d}", f"Owner M{i}", f"{100 + i} Madison St",
                        7000.0, True)
        for i in range(4)
    ] + [
        _FakeMadisonRec("M-04", "ACME LLC", "104 Madison Ave",
                        9000.0, False),                   # entity, drops at individuals
        _FakeMadisonRec("M-05", "Low Owner", "105 Madison Ave",
                        3000.0, True),                    # low balance, drops at min_balance
    ]
    jefferson_recs = [
        _FakeJeffersonRec(f"J-{i:02d}", f"Owner J{i}", f"{200 + i} Jeff Rd",
                          "Birmingham", "AL", "35023", 8000.0, True)
        for i in range(3)
    ] + [
        _FakeJeffersonRec("J-03", "Low Jeff", "203 Jeff Rd", "Birmingham",
                          "AL", "35023", 2000.0, True),    # low balance, drops at min_balance
    ]

    monkeypatch.setattr(pipeline, "_fetch_madison_raw", lambda: madison_recs)
    monkeypatch.setattr(pipeline, "_fetch_jefferson_raw", lambda: jefferson_recs)
    monkeypatch.setattr(pipeline, "_fetch_marshall_raw", lambda: [])

    # Mock the to_notice_data factories from the adapter modules. The pipeline
    # imports these lazily inside fetch_tax_distress, so we monkeypatch the
    # adapter modules themselves.
    import madison_tax_delinquent_api
    import jefferson_tax_delinquent_api
    monkeypatch.setattr(madison_tax_delinquent_api, "to_notice_data",
                        _fake_madison_to_notice_data)
    monkeypatch.setattr(jefferson_tax_delinquent_api, "to_notice_data",
                        _fake_jefferson_to_notice_data)

    # Smarty stub — 3 of 4 Madison survivors get an in-tier ZIP (35810 = T1
    # Huntsville per target_zips), 1 misses (empty ZIP). Records into the
    # supplied rate_tracker per CONTEXT.md D-04.
    smarty_call_count = {"n": 0}

    def fake_smarty_madison(situs, *, rate_tracker=None):
        smarty_call_count["n"] += 1
        # Drop the 4th Madison address — simulate a Smarty miss.
        if "103" in (situs or ""):
            if rate_tracker is not None:
                rate_tracker.record("smarty", False)
            return ("", "")
        if rate_tracker is not None:
            rate_tracker.record("smarty", True)
        return ("Huntsville", "35810")

    def fake_smarty_marshall(situs, *, rate_tracker=None):
        # No Marshall records in this fixture; defensive stub.
        if rate_tracker is not None:
            rate_tracker.record("smarty", False)
        return ("", "")

    import address_standardizer
    monkeypatch.setattr(address_standardizer, "smarty_zip_for_madison_address",
                        fake_smarty_madison)
    monkeypatch.setattr(address_standardizer, "smarty_zip_for_marshall_address",
                        fake_smarty_marshall)

    return {
        "state_file": state_file,
        "tmp_path": tmp_path,
        "smarty_call_count": smarty_call_count,
    }


# ── Test 1: all 5 D-01 gates populate ─────────────────────────────────


def test_tax_distress_funnel_records_all_gates(mocked_pipeline):
    """Drive a fully-mocked run; assert all 5 D-01 gates populated."""
    notices, funnel, rate_tracker = pipeline.fetch_tax_distress(
        counties=("Madison", "Jefferson"),
        individuals_only=True,
        min_balance=5000.0,
        stamp_auction_dates=False,
        tiers=(1, 2),
    )

    gates = funnel.as_ordered_dict()

    # D-01 invariant: pre-seeded gate order matches the canonical sequence.
    assert list(gates.keys()) == [
        "bulk_fetched", "individual_owner_filtered",
        "min_balance_filtered", "smarty_geocoded", "tier_gated",
    ]

    # bulk_fetched: 6 Madison raw + 4 Jefferson raw = 10
    assert gates["bulk_fetched"] == 10
    # individual_owner_filtered: drop 1 Madison entity (ACME LLC) → 9
    assert gates["individual_owner_filtered"] == 9
    # min_balance_filtered: drop 1 Madison + 1 Jefferson with $3K/$2K → 7
    assert gates["min_balance_filtered"] == 7
    # smarty_geocoded: 4 Madison survivors → 3 Smarty hits + 3 Jefferson
    # always-with-ZIP = 6 notices with resolved ZIP
    assert gates["smarty_geocoded"] == 6
    # tier_gated: 3 Madison-Smarty-hits in 35810 (T1) + 3 Jefferson in 35023
    # (T1) — 1 Madison Smarty miss had no ZIP and drops here = 6 in-tier
    assert gates["tier_gated"] == 6

    # Verify notices returned match the tier-gated count
    assert len(notices) == 6

    # Smarty rate tracker received 4 calls (one per Madison survivor)
    smarty_totals = rate_tracker.totals()["smarty"]
    assert smarty_totals == {"success": 3, "total": 4}


# ── Test 2: Slack blocks contain funnel + service-rates sections ─────


def test_tax_distress_slack_includes_funnel_and_rates_blocks(
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

    notices, funnel, rate_tracker = pipeline.fetch_tax_distress(
        counties=("Madison", "Jefferson"),
        individuals_only=True,
        min_balance=5000.0,
        stamp_auction_dates=False,
        tiers=(1, 2),
    )

    sent = pipeline.notify_slack(
        notices, funnel, rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # Should be 3 blocks: summary section + funnel + rates.
    assert len(captured["blocks"]) >= 3

    # Funnel block contains the pipeline name + at least one gate.
    funnel_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Funnel — tax_distress" in b.get("text", {}).get("text", "")
    )
    assert "bulk_fetched" in funnel_block_text
    assert "smarty_geocoded" in funnel_block_text
    assert "tier_gated" in funnel_block_text

    # Service-rates block contains all 4 service labels.
    rates_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Service Rates" in b.get("text", {}).get("text", "")
    )
    for label in ("2Captcha", "Smarty", "Tracerfy", "LLM"):
        assert label in rates_block_text

    # Per-run Smarty rate should be 75% today (3/4 hits).
    assert "75% today" in rates_block_text

    # Summary header contains the per-county counts.
    assert "Tax-Distress Run" in captured["text"]
    assert "Madison" in captured["text"] or "Jefferson" in captured["text"]


# ── Test 3: save_rolling_rates fires AFTER _send_blocks_webhook ──────


def test_tax_distress_save_rolling_rates_called_after_slack_post(
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

    notices, funnel, rate_tracker = pipeline.fetch_tax_distress(
        counties=("Madison", "Jefferson"),
        individuals_only=True,
        min_balance=5000.0,
        stamp_auction_dates=False,
        tiers=(1, 2),
    )

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
    # Smarty received 4 calls (3 success + 1 failure per fixture).
    assert totals["smarty"] == {"success": 3, "total": 4}
