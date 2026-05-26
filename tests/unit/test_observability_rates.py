"""Unit tests for ``src/observability.py`` rolling-rate persistence.

Covers ``load_rolling_rates()`` (missing/corrupt file → {} fallback),
``save_rolling_rates()`` (round-trip + same-date replacement + prune to
last 7 entries + atomic write + parent-dir creation), and
``rolling_rates_summary()`` (collapse multi-day entries into a single
per-service success/total ratio).

All tests use the ``tmp_path`` pytest fixture so the real
``output/observability/service_rates.json`` is NEVER touched.

Determinism: every save call passes an explicit ``today_date=`` arg so
tests don't depend on the system clock.
"""

from __future__ import annotations

import json
from pathlib import Path

from observability import (
    STATE_FILE,
    load_rolling_rates,
    rolling_rates_summary,
    save_rolling_rates,
)


# ── load_rolling_rates ───────────────────────────────────────────────


def test_load_rolling_rates_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing state file is silently treated as an empty rolling window."""
    missing = tmp_path / "does_not_exist.json"
    assert load_rolling_rates(missing) == {}


def test_load_rolling_rates_corrupt_json_returns_empty(tmp_path: Path) -> None:
    """A corrupt JSON file logs a warning and returns {} — DOES NOT raise."""
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("this is not json {{{", encoding="utf-8")
    assert load_rolling_rates(corrupt) == {}


def test_load_rolling_rates_reads_well_formed_file(tmp_path: Path) -> None:
    """A well-formed state file round-trips to the expected dict shape."""
    state = {
        "smarty": [
            {"date": "2026-05-20", "success": 8, "total": 10},
            {"date": "2026-05-21", "success": 7, "total": 9},
        ]
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    loaded = load_rolling_rates(path)
    assert loaded == state


# ── save_rolling_rates ───────────────────────────────────────────────


def test_save_rolling_rates_round_trip(tmp_path: Path) -> None:
    """Save + load yields the same {service: [entry]} shape."""
    path = tmp_path / "r.json"
    save_rolling_rates(
        {"smarty": {"success": 8, "total": 10}},
        today_date="2026-05-24",
        state_file=path,
    )

    loaded = load_rolling_rates(path)
    assert "smarty" in loaded
    assert loaded["smarty"] == [
        {"date": "2026-05-24", "success": 8, "total": 10}
    ]


def test_save_rolling_rates_same_date_replaces_entry(tmp_path: Path) -> None:
    """Two writes on the SAME date replace the first — no duplicates."""
    path = tmp_path / "r.json"
    save_rolling_rates(
        {"smarty": {"success": 1, "total": 5}},
        today_date="2026-05-24",
        state_file=path,
    )
    save_rolling_rates(
        {"smarty": {"success": 8, "total": 10}},
        today_date="2026-05-24",
        state_file=path,
    )

    loaded = load_rolling_rates(path)
    assert len(loaded["smarty"]) == 1
    assert loaded["smarty"][0] == {"date": "2026-05-24", "success": 8, "total": 10}


def test_save_rolling_rates_prunes_entries_older_than_7_days(tmp_path: Path) -> None:
    """Writing 15 distinct dates keeps only the most recent 7 per service."""
    path = tmp_path / "r.json"
    for day in range(10, 25):  # 15 distinct dates: 2026-05-10 .. 2026-05-24
        save_rolling_rates(
            {"smarty": {"success": 1, "total": 1}},
            today_date=f"2026-05-{day:02d}",
            state_file=path,
        )

    loaded = load_rolling_rates(path)
    assert "smarty" in loaded
    assert len(loaded["smarty"]) == 7
    dates = [e["date"] for e in loaded["smarty"]]
    # Earliest retained = 2026-05-18 (24 - 7 + 1)
    assert min(dates) == "2026-05-18"
    assert max(dates) == "2026-05-24"
    # Sorted ascending for readability
    assert dates == sorted(dates)


def test_save_rolling_rates_creates_parent_directory(tmp_path: Path) -> None:
    """Parent dir is created automatically — first-run scenario."""
    nested = tmp_path / "nested" / "deeper" / "state.json"
    assert not nested.parent.exists()

    save_rolling_rates(
        {"smarty": {"success": 1, "total": 1}},
        today_date="2026-05-24",
        state_file=nested,
    )

    assert nested.exists()
    assert nested.parent.is_dir()


def test_save_rolling_rates_multiple_services(tmp_path: Path) -> None:
    """A single save call can record multiple services in one shot."""
    path = tmp_path / "r.json"
    save_rolling_rates(
        {
            "smarty": {"success": 9, "total": 10},
            "2captcha": {"success": 5, "total": 5},
            "tracerfy": {"success": 2, "total": 6},
        },
        today_date="2026-05-24",
        state_file=path,
    )

    loaded = load_rolling_rates(path)
    assert loaded["smarty"] == [{"date": "2026-05-24", "success": 9, "total": 10}]
    assert loaded["2captcha"] == [{"date": "2026-05-24", "success": 5, "total": 5}]
    assert loaded["tracerfy"] == [{"date": "2026-05-24", "success": 2, "total": 6}]


def test_save_rolling_rates_returns_persisted_dict(tmp_path: Path) -> None:
    """The function returns the on-disk state for chained use."""
    path = tmp_path / "r.json"
    result = save_rolling_rates(
        {"smarty": {"success": 8, "total": 10}},
        today_date="2026-05-24",
        state_file=path,
    )

    assert "smarty" in result
    assert result["smarty"][0]["date"] == "2026-05-24"


def test_save_rolling_rates_preserves_other_services_on_write(tmp_path: Path) -> None:
    """Saving smarty's totals doesn't clobber an existing 2captcha entry."""
    path = tmp_path / "r.json"
    # Day 1: only 2captcha
    save_rolling_rates(
        {"2captcha": {"success": 3, "total": 4}},
        today_date="2026-05-23",
        state_file=path,
    )
    # Day 2: only smarty — 2captcha entry must survive
    save_rolling_rates(
        {"smarty": {"success": 8, "total": 10}},
        today_date="2026-05-24",
        state_file=path,
    )

    loaded = load_rolling_rates(path)
    assert "smarty" in loaded
    assert "2captcha" in loaded
    assert loaded["2captcha"][0]["success"] == 3


# ── rolling_rates_summary ────────────────────────────────────────────


def test_rolling_rates_summary_aggregates_across_days() -> None:
    """Summing across multiple daily entries gives total_success / total_total."""
    rolling = {
        "smarty": [
            {"date": "2026-05-20", "success": 4, "total": 5},
            {"date": "2026-05-21", "success": 3, "total": 5},
        ]
    }
    summary = rolling_rates_summary(rolling)
    assert summary["smarty"] == 0.7  # 7/10


def test_rolling_rates_summary_handles_zero_total() -> None:
    """A service with all-zero totals returns None, not a ZeroDivisionError."""
    rolling = {
        "tracerfy": [
            {"date": "2026-05-20", "success": 0, "total": 0},
        ]
    }
    summary = rolling_rates_summary(rolling)
    assert summary["tracerfy"] is None


def test_rolling_rates_summary_empty_input() -> None:
    """An empty rolling dict yields an empty summary."""
    assert rolling_rates_summary({}) == {}


# ── STATE_FILE constant ──────────────────────────────────────────────


def test_state_file_constant_is_under_output_observability() -> None:
    """STATE_FILE points to output/observability/service_rates.json (D-03)."""
    assert STATE_FILE == Path("output/observability/service_rates.json")
