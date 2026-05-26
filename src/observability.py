"""Pipeline-run observability primitives — funnel counters + service-rate tracking.

This module is the SHARED FOUNDATION for Phase 2 (Funnel Transparency). It
provides the pure-Python data structures every pipeline (APN scrape, APN
post-probate, pre-probate, Benchmark, tax-distress, code-violation) wires
into its end-of-run summary so the operator can see drop counts at every
gate and per-service success rates at a glance — both today's per-run rate
AND a 7-day rolling baseline.

Design decisions (from .planning/phases/02-funnel-transparency/02-CONTEXT.md):

- **D-01 (per-pipeline funnels)**: Each pipeline records its OWN gate
  sequence — insertion order matters. ``FunnelCounter`` uses an internal
  ``OrderedDict`` to preserve the gate order so the Slack block reads as a
  conversion funnel rather than an alphabetised dump.
- **D-02 (additive Slack helpers)**: This module exposes pure data
  structures — it knows NOTHING about Slack. The block-builder helpers
  live in ``slack_notifier`` and consume the ``OrderedDict`` /
  per-run-rates / rolling-rates dicts this module produces. Dependency
  direction is strictly one-way (slack_notifier → observability is not
  allowed; observability → slack_notifier IS NOT ALLOWED).
- **D-03 (rolling-window state file)**: 7-day rolling rates persist as
  JSON at ``output/observability/service_rates.json``. The shape is
  ``{service: [{date, success, total}, ...]}`` with one entry per service
  per day, pruned to the most recent 7 days on every write. Writes are
  atomic (``tmp + os.replace``) so a crash mid-write cannot leave the
  file half-flushed. Same-day writes REPLACE the existing entry — they
  never accumulate duplicates.
- **D-04 (4 tracked services)**: ``2captcha`` (solve rate), ``smarty``
  (delivery-line-1 hit rate), ``tracerfy`` (per-contact match rate),
  ``llm`` (structured-extraction success rate). Unknown service names
  passed to ``record()`` are silently dropped so call-site wrapping is
  defensive — a typo never explodes a daily run.

Tests: see ``tests/unit/test_observability_counters.py`` and
``tests/unit/test_observability_rates.py``. All file I/O in the test
suite uses ``tmp_path`` — the real ``output/observability/`` directory
is NEVER touched during tests.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Public constants ───────────────────────────────────────────────────

STATE_FILE: Path = Path("output/observability/service_rates.json")
"""On-disk location of the 7-day rolling rate state. Under ``output/``
which is already gitignored. Created on first ``save_rolling_rates()``
call (parent directory is auto-created)."""

TRACKED_SERVICES: tuple[str, ...] = ("2captcha", "smarty", "tracerfy", "llm")
"""Lowercase canonical service names. ``ServiceRateTracker.record()``
normalises caller-supplied names via ``.lower()`` so call sites can pass
``"Smarty"`` / ``"2Captcha"`` / etc. without breakage. Any name not in
this tuple is silently ignored."""


# ── FunnelCounter ──────────────────────────────────────────────────────


class FunnelCounter:
    """Ordered gate-count recorder for a single pipeline run.

    Each pipeline constructs ONE FunnelCounter per run, increments gate
    counters as records pass through each stage, then hands the
    ``as_ordered_dict()`` output to ``slack_notifier.build_funnel_block``
    for the end-of-run Slack post.

    Insertion order is the user-visible funnel order — gate names are
    rendered top-to-bottom in the Slack block exactly as they were first
    seen, so callers should either pre-seed via ``gates=[...]`` or set
    them in the order they actually fire in the pipeline.
    """

    def __init__(
        self,
        pipeline_name: str,
        gates: Iterable[str] | None = None,
    ) -> None:
        """Construct a fresh counter.

        Args:
            pipeline_name: Human-readable pipeline identifier (e.g.
                ``"apn_probate"``, ``"pre_probate"``, ``"benchmark"``).
                Surfaces in the Slack block header.
            gates: Optional iterable of gate names to pre-seed at 0. When
                supplied, the Slack block ALWAYS shows every gate — even
                if a stage emitted zero records — so the operator can
                instantly tell "all dropped at obit-match" apart from
                "stage never ran".
        """
        self._pipeline_name = pipeline_name
        self._counts: OrderedDict[str, int] = OrderedDict()
        if gates is not None:
            for gate in gates:
                self._counts[gate] = 0

    @property
    def pipeline_name(self) -> str:
        """The constructor-supplied pipeline identifier."""
        return self._pipeline_name

    def set(self, gate: str, count: int) -> None:
        """Idempotent record — last write wins.

        Preserves insertion order. A brand-new gate (not in the pre-seed
        list) is appended to the END so newly-added pipeline stages slot
        in naturally without disrupting the funnel order of existing
        gates.
        """
        self._counts[gate] = count

    def increment(self, gate: str, by: int = 1) -> None:
        """Bump (or initialise) a gate's count by ``by`` (default 1).

        Same insertion-order semantics as ``set`` — a brand-new gate
        gets appended to the end at the value of ``by``.
        """
        if gate in self._counts:
            self._counts[gate] += by
        else:
            self._counts[gate] = by

    def as_ordered_dict(self) -> OrderedDict[str, int]:
        """Return the gate sequence in insertion order for the Slack block.

        Returns a shallow copy so the caller can mutate freely without
        affecting subsequent ``increment``/``set`` calls on this counter.
        """
        return OrderedDict(self._counts)


# ── ServiceRateTracker ─────────────────────────────────────────────────


class ServiceRateTracker:
    """Per-run success/failure tally for the 4 tracked external services.

    Each pipeline constructs ONE tracker per run, calls ``record(service,
    success)`` at every external-service call site (or wraps the call
    site in a thin helper that does), then hands ``per_run_rates()`` to
    the Slack block builder.

    Unknown service names are silently dropped so callers can wrap any
    external call defensively — a typo or an unrecognised service tag
    never aborts a daily run.
    """

    def __init__(self) -> None:
        # Pre-seed all tracked services at 0/0 so totals() ALWAYS returns
        # all 4 services, even when a particular run made zero calls to
        # (say) the LLM. The Slack block then renders "LLM: n/a today"
        # rather than silently omitting the line.
        self._counts: dict[str, dict[str, int]] = {
            s: {"success": 0, "total": 0} for s in TRACKED_SERVICES
        }

    def record(self, service: str, success: bool) -> None:
        """Record one call's outcome against ``service``.

        Args:
            service: Service name. Case-insensitive (``.lower()``
                normalised internally). Must be one of
                ``TRACKED_SERVICES`` after normalisation — anything else
                is silently ignored (no log, no raise) so call sites can
                wrap external calls defensively.
            success: ``True`` if the call succeeded per the service's
                domain-specific success definition (see D-04 in
                02-CONTEXT.md), ``False`` otherwise.
        """
        key = service.lower()
        if key not in self._counts:
            # Defensive no-op — unknown service. Don't log either, this
            # is the documented contract: wrap any external call and let
            # the tracker swallow the unknown.
            return
        self._counts[key]["total"] += 1
        if success:
            self._counts[key]["success"] += 1

    def totals(self) -> dict[str, dict[str, int]]:
        """Return per-service ``{success, total}`` counts.

        Always contains all 4 tracked services (defaulting to
        ``{"success": 0, "total": 0}`` when untouched). Returns a deep
        copy so the caller can mutate freely.
        """
        return {s: dict(v) for s, v in self._counts.items()}

    def per_run_rates(self) -> dict[str, float | None]:
        """Return ``{service: success/total}`` for this run.

        Returns ``None`` for any service with ``total == 0`` — the Slack
        block renders that as ``"n/a today"`` rather than ``"0%"`` (a 0%
        rate is a real failure signal, "no calls made" is not).
        """
        out: dict[str, float | None] = {}
        for s, v in self._counts.items():
            if v["total"] == 0:
                out[s] = None
            else:
                out[s] = v["success"] / v["total"]
        return out


# ── Rolling-rate persistence ───────────────────────────────────────────


def load_rolling_rates(state_file: Path = STATE_FILE) -> dict[str, list[dict]]:
    """Load the persisted 7-day rolling state from disk.

    Returns ``{}`` (NOT raises) when the file is missing OR corrupt — the
    daily Slack post should always be able to render today's per-run
    rates even if the historical baseline has been wiped or scrambled.
    Corrupt-file cases are logged at WARNING level so they're visible in
    the daily run log without aborting the run.

    Returns:
        Dict of ``{service_name: [{"date": "YYYY-MM-DD", "success": int,
        "total": int}, ...]}``. Service names are lowercase per
        ``TRACKED_SERVICES``.
    """
    try:
        with state_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                # Defensive: a top-level JSON array or scalar is treated
                # like corruption. Same {} fallback, no raise.
                logger.warning(
                    "Rolling-rate state file %s has unexpected top-level "
                    "type %s; treating as empty",
                    state_file,
                    type(data).__name__,
                )
                return {}
            return data
    except FileNotFoundError:
        # First run — expected on a fresh checkout. Don't even warn.
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Could not read rolling-rate state file %s: %s — "
            "starting fresh for today's run",
            state_file,
            e,
        )
        return {}


def save_rolling_rates(
    today_totals: dict[str, dict[str, int]],
    *,
    today_date: str | None = None,
    state_file: Path = STATE_FILE,
    keep_days: int = 7,
) -> dict[str, list[dict]]:
    """Persist today's per-service totals into the rolling-window file.

    Read-modify-write: loads the existing state (or starts fresh on first
    run), upserts today's entry per service (same date REPLACES, never
    duplicates), prunes each service's history to the most recent
    ``keep_days`` entries, then writes the result atomically (tmp file +
    ``os.replace``) so a crash mid-write cannot half-flush the file.

    Args:
        today_totals: ``{service: {"success": int, "total": int}}`` —
            typically produced by ``ServiceRateTracker.totals()``.
        today_date: ISO ``YYYY-MM-DD``. Defaults to UTC today so the
            daily-post boundary is deterministic regardless of host TZ.
        state_file: Override for the on-disk path (used by tests via
            ``tmp_path``).
        keep_days: Window length; default 7 per D-03.

    Returns:
        The persisted dict (for chained use). Same shape as
        ``load_rolling_rates``.
    """
    if today_date is None:
        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Load existing (or empty on first run / corrupt file)
    state = load_rolling_rates(state_file)

    # 2. Upsert today's entry per service. Same-date entries are REPLACED,
    #    never duplicated — protects against a pipeline that re-runs mid-day
    #    and would otherwise double-count today's rates against the 7-day
    #    baseline.
    for service, counts in today_totals.items():
        entry = {
            "date": today_date,
            "success": int(counts.get("success", 0)),
            "total": int(counts.get("total", 0)),
        }
        existing = state.get(service, [])
        # Strip any existing entry with the same date
        existing = [e for e in existing if e.get("date") != today_date]
        existing.append(entry)
        # Sort by date descending, keep top N, re-sort ascending for
        # human readability in the JSON file.
        existing.sort(key=lambda e: e.get("date", ""), reverse=True)
        existing = existing[:keep_days]
        existing.sort(key=lambda e: e.get("date", ""))
        state[service] = existing

    # 3. Atomic write: ensure parent dir exists, write to tmp, rename.
    #    Mirrors src/config.py:save_state's pattern.
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)

    return state


def rolling_rates_summary(
    rolling: dict[str, list[dict]],
) -> dict[str, float | None]:
    """Collapse multi-day rolling entries into a single ratio per service.

    Sums ``success`` and ``total`` across all retained days for each
    service, then returns the ratio. Returns ``None`` for any service
    whose total across the window is 0 — the Slack block renders that as
    ``"— 7-day"`` (an em-dash) so "no historical baseline yet" looks
    different from "today's rate was 0%".
    """
    out: dict[str, float | None] = {}
    for service, entries in rolling.items():
        total_success = sum(int(e.get("success", 0)) for e in entries)
        total_total = sum(int(e.get("total", 0)) for e in entries)
        if total_total == 0:
            out[service] = None
        else:
            out[service] = total_success / total_total
    return out
