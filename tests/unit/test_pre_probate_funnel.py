"""pre_probate pipeline funnel + slack-blocks wiring tests (Phase 2, OPS-03 / OBS-01).

Drives pre_probate_pipeline_al through a fully-mocked simulated run (no
real legacy.com fetches, no real LLM, no real Smarty, no real Tracerfy,
no real Slack) and asserts:

  1. All 9 D-01 gates populate with the expected counts
  2. The Slack blocks payload contains the funnel block + service-rates
     block (D-02)
  3. save_rolling_rates fires exactly ONCE, AFTER _send_blocks_webhook
     returns True (rolling-rates ordering — D-03 / W6)

All file I/O routes through tmp_path so the real
``output/observability/`` directory is NEVER touched during tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pre_probate_pipeline_al as pipeline
import observability
from observability import FunnelCounter, ServiceRateTracker


# ── Test fixtures ─────────────────────────────────────────────────────


class _FakeHarvestedObit:
    """Minimal stand-in for obituary_harvester.HarvestedObit."""
    def __init__(self, url, source="legacy.com", county_hint="Jefferson"):
        self.url = url
        self.source = source
        self.county_hint = county_hint


class _FakeJeffersonRecord:
    """Stand-in for jefferson_property_api.JeffersonPropertyRecord."""
    def __init__(self, owner_name, parcel_number, situs_address, situs_city,
                 situs_zip, total_value=200000.0, is_homestead=True,
                 is_delinquent=False, municipality="Birmingham"):
        self.owner_name = owner_name
        self.parcel_number = parcel_number
        self.situs_address = situs_address
        self.situs_city = situs_city
        self.situs_zip = situs_zip
        self.total_value = total_value
        self.is_homestead = is_homestead
        self.is_delinquent = is_delinquent
        self.municipality = municipality


@pytest.fixture
def mocked_pre_probate(monkeypatch, tmp_path):
    """Wire monkeypatches for a 10-obit run that yields expected per-gate counts.

    Scenario (all gates D-01):
      - obits_harvested: 10
      - cross_source_deduped: 10 (no duplicates in this fixture)
      - fetched: 9 (1 obit fetch returns empty text → dropped_fetch_failed)
      - llm_extracted: 8 (1 LLM call rejects "not obituary")
      - dod_gated: 7 (1 stale DoD drops)
      - property_matched: 6 (1 not in any county API)
      - tier_gated: 4 (2 off-tier ZIP)
      - tracerfy_matched: 3 (4 submitted, 3 matched, populated by prepare_notices)
      - datasift_uploaded: 4 (CSV write count, stamped from _cli replication)
    """
    state_file = tmp_path / "service_rates.json"
    monkeypatch.setattr(observability, "STATE_FILE", state_file)

    # 10 fake harvested obits.
    obits = [_FakeHarvestedObit(f"https://legacy.com/person/x-{i}")
             for i in range(10)]
    monkeypatch.setattr(pipeline, "harvest_alabama",
                        lambda **kw: obits)

    # _fetch_full_obit_text: 9 return real text (≥200 chars per the
    # pipeline's length floor), 1 returns empty (fetch_failed).
    def fake_fetch(url):
        idx = int(url.rsplit("-", 1)[-1])
        if idx == 9:
            return ("", url)  # fetch_failed
        body = (
            f"Obit text {idx}. Survived by family. Born in Alabama. "
            f"Passed away peacefully surrounded by loved ones. "
            f"Memorial service to be held at local funeral home. "
            f"Beloved member of the community for many years. "
            f"Will be deeply missed by all who knew them."
        )
        return (body, url)

    monkeypatch.setattr(pipeline, "_fetch_full_obit_text", fake_fetch)

    # _extract_decedent_with_llm: 8 return valid extractions; idx 8 returns None
    # (LLM rejects), idx 0-6 return fresh DoDs, idx 7 returns a stale DoD.
    from datetime import datetime, timedelta

    fresh_dod = datetime.now().strftime("%Y-%m-%d")
    stale_dod = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")

    # Unique last names so the same-person dedup doesn't collapse them
    # (_normalize_decedent_key uses first-3-chars + last-name + DoD).
    _last_names = [
        "Anderson", "Bennett", "Carter", "Donovan", "Eastman",
        "Franklin", "Gilbert", "Hawkins",
    ]

    def fake_llm(text, api_key=None, rate_tracker=None):
        idx = int(text.split(".")[0].split()[-1])  # parse the "Obit text N" prefix
        if idx == 8:
            if rate_tracker is not None:
                rate_tracker.record("llm", False)
            return None
        if rate_tracker is not None:
            rate_tracker.record("llm", True)
        # idx 7 → stale DoD
        dod = stale_dod if idx == 7 else fresh_dod
        return pipeline.DecedentExtraction(
            is_obituary=True,
            decedent_full_name=f"Joseph {_last_names[idx]}",
            decedent_first_name="Joseph",
            decedent_last_name=_last_names[idx],
            decedent_age_at_death=75,
            date_of_death=dod,
            all_survivors=[{"name": "Jane Doe", "relationship": "daughter",
                            "city": "Birmingham"}],
            spouse_name="",
            preceded_in_death=[],
            executor_named="",
        )

    monkeypatch.setattr(pipeline, "_extract_decedent_with_llm", fake_llm)

    # _attach_property_for_decedent: idx 6 returns no property; the rest
    # return a Jefferson record. ZIPs: first 4 in-tier (35023 Hueytown T1,
    # 35226 Vestavia T1, 35023, 35226); idx 4+5 off-tier (35999, 35888).
    in_tier_zips = ["35023", "35226", "35023", "35226"]

    def fake_attach(ext, county_hint=""):
        # Pull the digit suffix off the last name for routing.
        idx = _last_names.index(ext.decedent_last_name)
        if idx == 6:
            return (None, [], "")
        if idx < 4:
            zip_code = in_tier_zips[idx]
        else:
            zip_code = "35999" if idx == 4 else "35888"
        rec = _FakeJeffersonRecord(
            owner_name=ext.decedent_full_name,
            parcel_number=f"PARCEL-{idx:04d}",
            situs_address=f"{100 + idx} Main St",
            situs_city="Birmingham",
            situs_zip=zip_code,
            total_value=250000.0,
        )
        return (rec, [rec], "Jefferson")

    monkeypatch.setattr(pipeline, "_attach_property_for_decedent", fake_attach)

    # Smarty stubs (shouldn't fire — Jefferson records have ZIP already).
    monkeypatch.setattr(pipeline, "_smarty_zip_for_madison_address",
                        lambda *a, **kw: ("", ""))
    monkeypatch.setattr(pipeline, "_smarty_zip_for_marshall_address",
                        lambda *a, **kw: ("", ""))

    # batch_skip_trace: 4 submitted (the 4 in-tier records), 3 matched.
    import tracerfy_skip_tracer

    def fake_batch_skip_trace(notices, **kw):
        tracker = kw.get("rate_tracker")
        if tracker is not None:
            for _ in range(3):
                tracker.record("tracerfy", True)
            tracker.record("tracerfy", False)
        return {
            "submitted": 4, "matched": 3, "phones_found": 5,
            "emails_found": 2, "cost": 0.08,
        }

    monkeypatch.setattr(
        tracerfy_skip_tracer, "batch_skip_trace", fake_batch_skip_trace,
    )

    monkeypatch.setattr(
        pipeline, "_promote_heir_contacts_to_csv_slots", lambda n: None,
    )

    # CSV writer no-op.
    import datasift_formatter
    monkeypatch.setattr(
        datasift_formatter, "write_datasift_csv",
        lambda notices, **kw: tmp_path / "fake.csv",
    )

    return {"obits": obits, "state_file": state_file, "tmp_path": tmp_path}


# ── Test 1: All 9 gates populated ─────────────────────────────────────


def test_pre_probate_funnel_records_all_gates(mocked_pre_probate):
    """Drive a fully-mocked run; assert all 9 D-01 gates populated."""
    results, funnel, rate_tracker = pipeline.run_pipeline(
        limit=50, pages=1, tier_filter=(1, 2),
    )

    # Run prepare_notices → populates tracerfy_matched gate.
    notices, stats = pipeline.prepare_notices(
        results, skip_trace=True, funnel=funnel, rate_tracker=rate_tracker,
    )

    # Replicate _cli's datasift_uploaded gate-stamp.
    funnel.set("datasift_uploaded", len(notices))

    gates = funnel.as_ordered_dict()

    # Insertion-order assertion: all 9 D-01 gates in sequence.
    assert list(gates.keys()) == [
        "obits_harvested", "cross_source_deduped", "fetched",
        "llm_extracted", "dod_gated", "property_matched",
        "tier_gated", "tracerfy_matched", "datasift_uploaded",
    ]

    assert gates["obits_harvested"] == 10
    # No duplicates in this fixture so cross_source_deduped == 10.
    assert gates["cross_source_deduped"] == 10
    # 9 obits returned non-empty text (idx 9 fetched empty → dropped).
    assert gates["fetched"] == 9
    # 8 LLM extractions succeeded (idx 8 returned None — not_obituary).
    assert gates["llm_extracted"] == 8
    # 7 passed DoD freshness (idx 7 had stale DoD).
    assert gates["dod_gated"] == 7
    # 6 matched a property (idx 6 returned None from attach).
    assert gates["property_matched"] == 6
    # 4 in-tier (idx 4 + 5 off-tier).
    assert gates["tier_gated"] == 4
    # 3 of 4 Tracerfy contacts matched.
    assert gates["tracerfy_matched"] == 3
    # 4 written to CSV.
    assert gates["datasift_uploaded"] == 4


# ── Test 2: Slack blocks contain funnel + service-rates sections ─────


def test_pre_probate_slack_includes_funnel_and_rates_blocks(
    mocked_pre_probate, monkeypatch,
):
    """Capture the blocks payload sent to _send_blocks_webhook and assert
    it contains Funnel (pre_probate) + Service Rates sections."""
    captured: dict = {}

    def fake_send(text, blocks, webhook_url=None):
        captured["text"] = text
        captured["blocks"] = blocks
        captured["webhook_url"] = webhook_url
        return True

    monkeypatch.setattr(pipeline, "_send_blocks_webhook", fake_send)
    monkeypatch.setattr(pipeline, "save_rolling_rates", lambda totals, **kw: {})

    results, funnel, rate_tracker = pipeline.run_pipeline(
        limit=50, pages=1, tier_filter=(1, 2),
    )
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

    # 3 blocks: summary + funnel + rates.
    assert len(captured["blocks"]) >= 3

    # Funnel block contains the pipeline name + at least 2 D-01 gate names.
    funnel_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Funnel — pre_probate" in b.get("text", {}).get("text", "")
    )
    assert "obits_harvested" in funnel_block_text
    assert "llm_extracted" in funnel_block_text
    assert "tracerfy_matched" in funnel_block_text

    # Service-rates block contains all 4 service labels.
    rates_block_text = next(
        b["text"]["text"] for b in captured["blocks"]
        if "Service Rates" in b.get("text", {}).get("text", "")
    )
    for label in ("2Captcha", "Smarty", "Tracerfy", "LLM"):
        assert label in rates_block_text


# ── Test 3: save_rolling_rates fires AFTER _send_blocks_webhook ──────


def test_pre_probate_save_rolling_rates_called_after_slack_post(
    mocked_pre_probate, monkeypatch,
):
    """Rolling-rates ordering: load BEFORE blocks build (implicit, via
    rolling_rates_summary call), save AFTER successful _send_blocks_webhook.
    This test asserts send → save call ordering."""
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

    results, funnel, rate_tracker = pipeline.run_pipeline(
        limit=50, pages=1, tier_filter=(1, 2),
    )
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

    # send MUST precede save (exactly once each).
    assert call_order == ["send", "save"]

    # save received the tracker's per-service totals.
    totals = save_args["totals"]
    assert set(totals.keys()) == {"2captcha", "smarty", "tracerfy", "llm"}
    # Tracerfy: 4 calls (3 success + 1 fail per the fixture).
    assert totals["tracerfy"] == {"success": 3, "total": 4}
    # LLM: 9 calls (8 success + 1 fail — fixture records on every fake_llm
    # invocation, including idx 8 which returns None).
    assert totals["llm"] == {"success": 8, "total": 9}
