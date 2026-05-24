"""benchmark_pipeline_al funnel + slack-blocks wiring tests (Phase 2, OPS-03 / OBS-01).

Drives benchmark_pipeline_al through a fully-mocked simulated run (no
real Benchmark Web login, no real Jefferson property API, no real
obituary search, no real Tracerfy, no real Slack) and asserts:

  1. All 6 benchmark gates populate with the expected counts after a
     synthetic 10-case run (per the additive scope documented in
     02-04 must_haves — WARNING 8)
  2. The Slack blocks payload contains the legacy summary section +
     funnel block + service-rates block (D-02 — one message, three
     blocks)
  3. save_rolling_rates fires exactly ONCE, AFTER _send_blocks_webhook
     returns True (rolling-rates ordering — D-03 / W6)

All file I/O (observability.STATE_FILE) routes through tmp_path so the
real ``output/observability/`` directory is NEVER touched during tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import benchmark_pipeline_al as pipeline
import observability
from benchmark_obituary_match import MatchResult
from benchmark_web import BenchmarkCase, BenchmarkParty
from observability import FunnelCounter, ServiceRateTracker


# ── Test fixtures ─────────────────────────────────────────────────────


def _make_benchmark_case(idx: int, decedent_last: str) -> BenchmarkCase:
    """Build a hydrated BenchmarkCase fixture with decedent + petitioner
    parties populated so .decedent_name and .petitioner_name return
    non-empty strings without hitting any property API."""
    case = BenchmarkCase(
        case_number=f"26BHM{idx:06d}",
        case_url=f"https://benchmarkweb.test/case/{idx}",
        file_date="2026-05-20",
        case_type="ESTATE",
        court_type="BIRMINGHAM",
        status="OPEN",
        judge="BLANCHARD, YASHIBA",
        parties=[
            BenchmarkParty(
                party_type="DECEDENT",
                name=f"{decedent_last.upper()}, JOSEPH ALAN",
            ),
            BenchmarkParty(
                party_type="PETITIONER",
                name=f"{decedent_last.upper()}, JANE MARIE",
            ),
            BenchmarkParty(
                party_type="ATTORNEY",
                name="SMITH, JOHN ESQ",
            ),
        ],
    )
    return case


class _FakeBenchmarkSession:
    """Replaces BenchmarkSession so no real login + no real browser fires."""

    def __init__(self, cases: list[BenchmarkCase], *, headless: bool = True):
        self._cases = cases

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def list_cases_in_date_range(self, start, end):
        return list(self._cases)

    async def fetch_case_detail(self, url, case_number):
        for c in self._cases:
            if c.case_number == case_number:
                return c
        raise KeyError(case_number)


@pytest.fixture
def mocked_benchmark_pipeline(monkeypatch, tmp_path):
    """Pre-wire monkeypatches that every benchmark funnel test needs.

    Scenario: 10 cases pulled. First 8 get a Jefferson parcel
    (`property_found`). Of those 8, 6 land in a Tier-1 ZIP; 2 land in
    an off-tier ZIP and drop. Of the 6 in-tier cases, 1 has a fiduciary
    petitioner (appears in 2+ cases via the synthetic petitioner-share
    pattern) and skips obit; the other 5 get an obituary match — 3 at
    `medium` confidence, 2 at `none`. Tracerfy matches 4 of 5 enriched.
    CSV writes the 5 enriched notices.
    """
    state_file = tmp_path / "service_rates.json"
    monkeypatch.setattr(observability, "STATE_FILE", state_file)

    # Build 10 cases. The 7th case (idx=6) shares a petitioner with the
    # 6th (idx=5) so _classify_fiduciaries flags both as fiduciary
    # (appears in 2+ cases). We'll arrange the synthetic property /
    # tier outcomes to put both in-tier so one slot routes to
    # "skipped_fiduciary" cleanly.
    last_names = [
        "Alpha", "Bravo", "Charlie", "Delta", "Echo",
        "Foxtrot", "Foxtrot",  # idx 5 + 6 share — fiduciary
        "Hotel", "India", "Juliet",
    ]
    cases = [_make_benchmark_case(i, last_names[i]) for i in range(10)]

    # Replace BenchmarkSession with the fake. The pipeline uses
    # `async with BenchmarkSession(...)` directly.
    monkeypatch.setattr(
        pipeline, "BenchmarkSession",
        lambda *a, **kw: _FakeBenchmarkSession(cases, **kw),
    )

    # Property locator: first 8 get parcels; first 6 in-tier (T1 35023
    # Hueytown), positions 6–7 off-tier (35999), positions 8–9 NO
    # property → status "dropped_no_property".
    fake_records = []  # Not actually used; _attach_property is stubbed
    in_tier_zip = "35023"
    off_tier_zip = "35999"

    def fake_attach_property(case, decedent_human, min_score=0.5):
        idx = int(case.case_number[-6:])
        if idx >= 8:
            return (None, [])
        # Build a fake JeffersonPropertyRecord shape — only the fields
        # _attach_property writes back onto the CaseResult matter.
        class _FakeRec:
            def __init__(self, idx):
                self.parcel_number = f"PARCEL-{idx:04d}"
                self.situs_address = f"{100 + idx} Main St"
                self.situs_city = "Birmingham"
                self.situs_zip = in_tier_zip if idx < 6 else off_tier_zip
                self.is_homestead = (idx % 2 == 0)
                self.total_value = 150_000.0 + idx * 10_000
                self.is_delinquent = False
                self.municipality = "BHAM"
        rec = _FakeRec(idx)
        return (rec, [rec])

    monkeypatch.setattr(pipeline, "_attach_property", fake_attach_property)

    # Obituary match: of the 5 non-fiduciary enriched cases (indices
    # 0,1,2,3,4 — idx 5+6 are the fiduciary pair, idx 6 is off-tier
    # AND fiduciary so drops at tier_filter first), we want 3 at
    # `medium` and 2 at `none` so obituary_confirmed = 3.
    def fake_match_petitioner_city(case, **kwargs):
        idx = int(case.case_number[-6:])
        # rate_tracker propagation simulation — the real Wave 2 LLM
        # path records into the tracker via chat_json; replicate here
        # so test 3's totals() assertion passes.
        tracker = kwargs.get("rate_tracker")
        if tracker is not None:
            # Each obit-match attempt = 1 LLM call (success/failure
            # based on whether the parsed JSON has the required keys).
            confidence = "medium" if idx < 3 else "none"
            tracker.record("llm", confidence == "medium")
        m = MatchResult(
            case_number=case.case_number,
            decedent_name=case.decedent_name,
            petitioner_name=case.petitioner_name,
        )
        m.confidence = "medium" if idx < 3 else "none"
        m.petitioner_match = "fuzzy" if idx < 3 else "not_found"
        m.decedent_full_name = case.decedent_name
        m.decedent_city = "Birmingham"
        m.decedent_state = "AL"
        return m

    monkeypatch.setattr(
        pipeline, "match_petitioner_city", fake_match_petitioner_city,
    )

    # Tracerfy: 5 submitted, 4 matched. Honours rate_tracker contract
    # so the totals() assertion in Test 3 passes.
    import tracerfy_skip_tracer

    def fake_batch_skip_trace(notices, **kw):
        tracker = kw.get("rate_tracker")
        if tracker is not None:
            for _ in range(4):
                tracker.record("tracerfy", True)
            for _ in range(1):
                tracker.record("tracerfy", False)
        return {
            "submitted": 5, "matched": 4, "phones_found": 7,
            "emails_found": 3, "cost": 0.10,
        }

    monkeypatch.setattr(
        tracerfy_skip_tracer, "batch_skip_trace", fake_batch_skip_trace,
    )

    # CSV writer: no-op, return a fake path.
    import datasift_formatter
    monkeypatch.setattr(
        datasift_formatter, "write_datasift_csv",
        lambda notices, **kw: tmp_path / "fake.csv",
    )

    # Defang heir-promotion (touches NoticeData internals not relevant
    # to the funnel assertion).
    monkeypatch.setattr(
        pipeline, "_promote_heir_contacts_to_csv_slots", lambda n: None,
    )

    return {
        "cases": cases,
        "state_file": state_file,
        "tmp_path": tmp_path,
    }


# ── Test 1: All 6 gates populated ─────────────────────────────────────


def test_benchmark_funnel_records_all_gates(mocked_benchmark_pipeline):
    """Drive a fully-mocked run; assert all 6 D-01-additive gates populated."""
    results, funnel, rate_tracker = asyncio.run(pipeline.run_pipeline(
        days_back=7,
        max_cases=20,
        tier_filter=(1, 2),
    ))

    # Run prepare_notices → exercises the Tracerfy + tracerfy_matched gate.
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )

    # Stamp datasift_uploaded gate (the _cli path does this after CSV
    # write — replicate here for the assertion).
    funnel.set("datasift_uploaded", len(notices))

    gates = funnel.as_ordered_dict()

    # Insertion-order assertion: pipeline.run_pipeline pre-seeds the
    # full 6-gate benchmark sequence.
    assert list(gates.keys()) == list(pipeline.BENCHMARK_GATES)

    # pulled: scrape returned 10 cases
    assert gates["pulled"] == 10
    # tier_gated: 6 in-tier (positions 0–5; positions 6–7 off-tier;
    # positions 8–9 no property)
    assert gates["tier_gated"] == 6
    # fiduciary_filtered: of the 6 in-tier, position 5 routes to
    # "skipped_fiduciary" (Foxtrot shared petitioner with position 6).
    # So 5 enriched survive to obit.
    assert gates["fiduciary_filtered"] == 5
    # obituary_confirmed: idx 0,1,2 → medium confidence; idx 3,4 →
    # none. So 3 confirmed.
    assert gates["obituary_confirmed"] == 3
    # tracerfy_matched: fake_batch_skip_trace returned matched=4
    assert gates["tracerfy_matched"] == 4
    # datasift_uploaded: 5 enriched notices written
    assert gates["datasift_uploaded"] == 5


# ── Test 2: Slack blocks contain funnel + service-rates sections ─────


def test_benchmark_slack_includes_funnel_and_rates_blocks(
    mocked_benchmark_pipeline, monkeypatch,
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
    # mocked send-success.
    monkeypatch.setattr(pipeline, "save_rolling_rates", lambda totals, **kw: {})

    results, funnel, rate_tracker = asyncio.run(pipeline.run_pipeline(
        days_back=7,
        max_cases=20,
        tier_filter=(1, 2),
    ))
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )
    funnel.set("datasift_uploaded", len(notices))

    sent = pipeline.notify_slack(
        results,
        skip_trace_stats=stats,
        days_back=7,
        funnel=funnel,
        rate_tracker=rate_tracker,
        webhook_url="https://example.test/webhook",
    )
    assert sent is True

    # Should be 3 blocks: existing summary section + funnel + rates.
    assert len(captured["blocks"]) == 3

    # Funnel block contains the pipeline name + at least one gate.
    funnel_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Funnel — benchmark" in b.get("text", {}).get("text", "")
    )
    for gate in pipeline.BENCHMARK_GATES:
        assert gate in funnel_block_text, f"funnel block missing gate {gate!r}"

    # Service-rates block contains all 4 service labels.
    rates_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Service Rates" in b.get("text", {}).get("text", "")
    )
    for label in ("2Captcha", "Smarty", "Tracerfy", "LLM"):
        assert label in rates_block_text


# ── Test 3: save_rolling_rates fires AFTER _send_blocks_webhook ──────


def test_benchmark_save_rolling_rates_called_after_slack_post(
    mocked_benchmark_pipeline, monkeypatch,
):
    """Rolling-rates ordering (D-03 / W6):
    load_rolling_rates BEFORE the blocks build, save_rolling_rates AFTER
    a successful Slack post. Asserts the send → save sequence via call-
    order capture, and confirms the tracker's totals() reach save."""
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
        days_back=7,
        max_cases=20,
        tier_filter=(1, 2),
    ))
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )
    funnel.set("datasift_uploaded", len(notices))

    sent = pipeline.notify_slack(
        results,
        skip_trace_stats=stats,
        days_back=7,
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
    # tracerfy received 5 calls (4 success + 1 failure per mocked
    # batch_skip_trace fixture above).
    assert totals["tracerfy"] == {"success": 4, "total": 5}
    # llm received 5 calls (3 medium + 2 none per mocked
    # match_petitioner_city fixture).
    assert totals["llm"] == {"success": 3, "total": 5}
