"""Unit tests for ``src/observability.py`` counter primitives.

Covers:
- ``FunnelCounter`` insertion-order preservation, ``set``/``increment``
  semantics, late-added gates appended at the end, and ``as_ordered_dict``
  output shape.
- ``ServiceRateTracker`` success/failure tally for the 4 tracked services,
  unknown-service silent-no-op behaviour, and per-run rate computation
  (including the ``None`` case when total==0).

These tests are foundation-only — no Slack code, no HTTP, no file I/O.
The companion file ``test_observability_rates.py`` covers the JSON state-
file persistence layer with ``tmp_path`` isolation.
"""

from __future__ import annotations

from collections import OrderedDict

from observability import (
    FunnelCounter,
    ServiceRateTracker,
    TRACKED_SERVICES,
)


# ── FunnelCounter ────────────────────────────────────────────────────


def test_funnel_counter_ordered_increment() -> None:
    """Pre-seed with gates list, set/increment, verify ordered output."""
    counter = FunnelCounter("apn_probate", gates=["scraped", "deduped", "tier_gated"])

    # Pre-seeded gates all default to 0
    assert counter.as_ordered_dict() == OrderedDict(
        [("scraped", 0), ("deduped", 0), ("tier_gated", 0)]
    )
    assert counter.pipeline_name == "apn_probate"

    # set() replaces value, preserves insertion order
    counter.set("deduped", 5)
    assert counter.as_ordered_dict() == OrderedDict(
        [("scraped", 0), ("deduped", 5), ("tier_gated", 0)]
    )

    # increment(by=N) then increment() (default by=1) accumulates
    counter.increment("tier_gated", 3)
    counter.increment("tier_gated")
    assert counter.as_ordered_dict()["tier_gated"] == 4


def test_funnel_counter_late_gate_appended_at_end() -> None:
    """A brand-new gate added via .set() after construction goes to the end."""
    counter = FunnelCounter("pre_probate", gates=["scraped", "deduped"])
    counter.set("uploaded", 7)  # NEW gate not in the pre-seed list

    keys = list(counter.as_ordered_dict().keys())
    assert keys == ["scraped", "deduped", "uploaded"]
    assert counter.as_ordered_dict()["uploaded"] == 7


def test_funnel_counter_no_preseed_uses_insertion_order() -> None:
    """When gates=None, the counter records gates in the order they're first touched."""
    counter = FunnelCounter("benchmark")
    counter.increment("scraped", 10)
    counter.set("zip_gated", 4)
    counter.increment("tracerfy_matched", 1)

    keys = list(counter.as_ordered_dict().keys())
    assert keys == ["scraped", "zip_gated", "tracerfy_matched"]


def test_funnel_counter_set_is_last_write_wins() -> None:
    """set() is idempotent — last write wins, doesn't accumulate."""
    counter = FunnelCounter("p", gates=["a"])
    counter.set("a", 3)
    counter.set("a", 7)
    assert counter.as_ordered_dict()["a"] == 7


def test_funnel_counter_pipeline_name_property() -> None:
    """``pipeline_name`` exposes the constructor arg."""
    counter = FunnelCounter("tax_distress")
    assert counter.pipeline_name == "tax_distress"


# ── ServiceRateTracker ───────────────────────────────────────────────


def test_service_rate_tracker_records_success_and_failure() -> None:
    """record() accumulates success + total per service correctly."""
    tracker = ServiceRateTracker()
    tracker.record("smarty", True)
    tracker.record("smarty", False)

    totals = tracker.totals()
    assert totals["smarty"] == {"success": 1, "total": 2}


def test_service_rate_tracker_single_success() -> None:
    """One successful call yields 1/1."""
    tracker = ServiceRateTracker()
    tracker.record("2captcha", True)

    assert tracker.totals()["2captcha"] == {"success": 1, "total": 1}


def test_service_rate_tracker_ignores_unknown_service() -> None:
    """An unknown service name is silently no-op'd, NEVER raises."""
    tracker = ServiceRateTracker()
    # No exception — defensive call-site wrapping works
    tracker.record("unknown_service", True)
    tracker.record("anthropic_haiku_v3", False)

    totals = tracker.totals()
    assert "unknown_service" not in totals
    assert "anthropic_haiku_v3" not in totals
    # Sanity — tracked services still present at zero
    for s in TRACKED_SERVICES:
        assert totals[s] == {"success": 0, "total": 0}


def test_service_rate_tracker_all_tracked_services_present() -> None:
    """totals() always returns all 4 tracked services, defaulting to 0/0."""
    tracker = ServiceRateTracker()
    totals = tracker.totals()
    assert set(totals.keys()) == set(TRACKED_SERVICES)
    for s in TRACKED_SERVICES:
        assert totals[s] == {"success": 0, "total": 0}


def test_service_rate_tracker_per_run_rates_none_for_zero_total() -> None:
    """per_run_rates() returns None when total==0 (not 0.0, not a divide-by-zero)."""
    tracker = ServiceRateTracker()
    rates = tracker.per_run_rates()
    for s in TRACKED_SERVICES:
        assert rates[s] is None


def test_service_rate_tracker_per_run_rates_computes_ratio() -> None:
    """per_run_rates() returns success / total when total>0."""
    tracker = ServiceRateTracker()
    tracker.record("smarty", True)
    tracker.record("smarty", False)  # 1/2 = 0.5

    rates = tracker.per_run_rates()
    assert rates["smarty"] == 0.5


def test_service_rate_tracker_case_insensitive_normalization() -> None:
    """Callers may pass 'Smarty' or '2Captcha' — internal storage is lowercased."""
    tracker = ServiceRateTracker()
    tracker.record("Smarty", True)
    tracker.record("2Captcha", True)
    tracker.record("TRACERFY", False)
    tracker.record("LLM", True)

    totals = tracker.totals()
    assert totals["smarty"] == {"success": 1, "total": 1}
    assert totals["2captcha"] == {"success": 1, "total": 1}
    assert totals["tracerfy"] == {"success": 0, "total": 1}
    assert totals["llm"] == {"success": 1, "total": 1}
