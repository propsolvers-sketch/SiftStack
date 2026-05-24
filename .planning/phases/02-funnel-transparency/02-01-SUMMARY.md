---
phase: 02-funnel-transparency
plan: 02-01
subsystem: observability
tags:
  - observability
  - slack
  - funnel
  - foundation
requirements:
  - OPS-03
  - OBS-01
dependency_graph:
  requires: []
  provides:
    - "src/observability.py::FunnelCounter"
    - "src/observability.py::ServiceRateTracker"
    - "src/observability.py::load_rolling_rates"
    - "src/observability.py::save_rolling_rates"
    - "src/observability.py::rolling_rates_summary"
    - "src/observability.py::STATE_FILE"
    - "src/observability.py::TRACKED_SERVICES"
    - "src/slack_notifier.py::build_funnel_block"
    - "src/slack_notifier.py::build_service_rates_block"
  affects:
    - "output/observability/service_rates.json (created on first save call)"
tech_stack:
  added:
    - "collections.OrderedDict (insertion-order gate sequence)"
    - "datetime.timezone (UTC daily boundaries)"
    - "json + os.replace (atomic state-file writes)"
  patterns:
    - "Atomic tmp+rename JSON write (mirrors src/config.py:save_state)"
    - "Lowercase service-name normalisation in record() (defensive call-site wrapping)"
    - "Pre-seeded gates so zero-count stages stay visible in Slack block"
    - "None vs 0.0 distinction (no-data vs all-failed) in per-run rates"
key_files:
  created:
    - "src/observability.py (344 lines)"
    - "tests/unit/test_observability_counters.py (155 lines)"
    - "tests/unit/test_observability_rates.py (190 lines)"
    - "tests/unit/test_slack_funnel_blocks.py (211 lines)"
  modified:
    - "src/slack_notifier.py (additive only — 113 lines appended; existing functions byte-identical)"
decisions:
  - "D-01 honored: per-pipeline funnels — FunnelCounter uses OrderedDict so each pipeline's gate sequence renders top-to-bottom in the Slack block, no cross-pipeline normalisation"
  - "D-02 honored: build_funnel_block / build_service_rates_block APPENDED to slack_notifier.py; existing send_slack_notification + _send_webhook + notify_error + notify_warning + notify_preflight_failure + build_summary untouched (zero deletions verified)"
  - "D-03 honored: STATE_FILE = Path('output/observability/service_rates.json'); atomic tmp+rename write; 7-day prune-on-write; same-date entries REPLACED (no duplicates)"
  - "D-04 honored: TRACKED_SERVICES = ('2captcha', 'smarty', 'tracerfy', 'llm'); unknown service names silently no-op (defensive)"
  - "Dependency direction strictly one-way: observability.py has NO imports from slack_notifier (verified via grep). Wave 2/3 plans should import directly from observability — no wrapper needed"
  - "UTC default for today_date in save_rolling_rates so daily-post boundaries are deterministic regardless of host TZ"
  - "per_run_rates() returns None (not 0.0) when total==0 — the Slack block renders that as 'n/a today' so 'no calls made' is distinguishable from 'all calls failed'"
metrics:
  duration: "~22 min"
  completed: "2026-05-24"
  test_count: 38
---

# Phase 2 Plan 01: Observability Foundation Summary

Shipped the shared observability primitives (FunnelCounter, ServiceRateTracker, rolling-rate persistence) and the two Slack Block Kit helpers Wave 2 (02-02) and Wave 3 (02-03/04/05) depend on — pure data structures with strict one-way dependency direction and 38 offline tests, zero Slack HTTP calls, zero writes to the real state file during the test run.

## What Was Built

### `src/observability.py` — public API surface

```python
# Constants
STATE_FILE: Path = Path("output/observability/service_rates.json")
TRACKED_SERVICES: tuple[str, ...] = ("2captcha", "smarty", "tracerfy", "llm")

# FunnelCounter
class FunnelCounter:
    def __init__(self, pipeline_name: str, gates: Iterable[str] | None = None) -> None
    @property
    def pipeline_name(self) -> str
    def set(self, gate: str, count: int) -> None
    def increment(self, gate: str, by: int = 1) -> None
    def as_ordered_dict(self) -> OrderedDict[str, int]

# ServiceRateTracker
class ServiceRateTracker:
    def __init__(self) -> None
    def record(self, service: str, success: bool) -> None      # unknown service → silent no-op
    def totals(self) -> dict[str, dict[str, int]]               # always all 4 services, default 0/0
    def per_run_rates(self) -> dict[str, float | None]          # None when total==0

# Rolling-rate persistence
def load_rolling_rates(state_file: Path = STATE_FILE) -> dict[str, list[dict]]
def save_rolling_rates(
    today_totals: dict[str, dict[str, int]],
    *,
    today_date: str | None = None,    # ISO YYYY-MM-DD; defaults to UTC today
    state_file: Path = STATE_FILE,
    keep_days: int = 7,
) -> dict[str, list[dict]]
def rolling_rates_summary(rolling: dict[str, list[dict]]) -> dict[str, float | None]
```

### `src/slack_notifier.py` — appended helpers (additive only)

```python
def build_funnel_block(
    pipeline_name: str,
    gate_counts: dict,             # plain dict OR OrderedDict — insertion order preserved
) -> dict
# Returns:
# {"type": "section",
#  "text": {"type": "mrkdwn",
#           "text": "*Funnel — apn_probate*\n• scraped: 47\n• deduped: 42\n..."}}

def build_service_rates_block(
    per_run_rates: dict,           # {service_lower: float | None}
    rolling_rates: dict,           # {service_lower: float | None}
) -> dict
# Returns:
# {"type": "section",
#  "text": {"type": "mrkdwn",
#           "text": "*Service Rates*\n• 2Captcha: 100% today | 99% 7-day\n• Smarty: 86% today | 92% 7-day\n• Tracerfy: n/a today | 41% 7-day\n• LLM: 95% today | — 7-day"}}
```

Module constant `_RATE_DISPLAY_ORDER` pins service display order to `2Captcha → Smarty → Tracerfy → LLM`.

## JSON Schema — `output/observability/service_rates.json`

One JSON object at the root. Keys are lowercase service names (`TRACKED_SERVICES`). Each value is a list of daily entries, sorted ascending by date, pruned to the most recent 7 days. Example:

```json
{
  "smarty": [
    {"date": "2026-05-18", "success": 9, "total": 10},
    {"date": "2026-05-19", "success": 7, "total": 8},
    {"date": "2026-05-20", "success": 10, "total": 11},
    {"date": "2026-05-21", "success": 8, "total": 9},
    {"date": "2026-05-22", "success": 9, "total": 10},
    {"date": "2026-05-23", "success": 7, "total": 9},
    {"date": "2026-05-24", "success": 8, "total": 10}
  ],
  "2captcha": [
    {"date": "2026-05-24", "success": 14, "total": 14}
  ],
  "tracerfy": [
    {"date": "2026-05-24", "success": 2, "total": 6}
  ],
  "llm": [
    {"date": "2026-05-24", "success": 18, "total": 19}
  ]
}
```

Write semantics:
- **Atomic.** Each `save_rolling_rates()` writes to `service_rates.json.tmp` then `os.replace()`s into place — a crash mid-write cannot leave the file half-flushed.
- **Same-day replacement.** Two writes on the same `today_date` replace the first entry (no duplicates).
- **Prune on write.** Each service's list is sorted by date descending, sliced to `keep_days=7`, then re-sorted ascending for human-readable JSON output.
- **Parent dir auto-created.** First-run scenario (no `output/observability/` yet) handled by `mkdir(parents=True, exist_ok=True)`.
- **Missing/corrupt → empty.** `load_rolling_rates()` returns `{}` (logged at WARNING) when file is missing or JSON-corrupt — never raises, so a wiped state file cannot abort a daily run.

## Wiring Guidance for Wave 2 / Wave 3

Plans 02-02 (service-call-site instrumentation) and 02-03/04/05 (pipeline orchestrator wiring) import **directly** from these modules. No further wrapper or factory is needed. Reference pattern:

```python
# In each pipeline's notify_slack() or main run loop:
from observability import (
    FunnelCounter,
    ServiceRateTracker,
    load_rolling_rates,
    save_rolling_rates,
    rolling_rates_summary,
)
from slack_notifier import build_funnel_block, build_service_rates_block

# Setup at run start:
funnel = FunnelCounter("apn_probate", gates=["scraped", "deduped", "tier_gated", "matched", "uploaded"])
rates = ServiceRateTracker()

# During the run:
funnel.set("scraped", len(raw_notices))
# ... after each enrichment stage ...
funnel.set("tier_gated", len(in_tier))
# At every external service call site (Wave 2 wires these):
rates.record("smarty", standardize_response.has_delivery_line_1)

# At run end:
today_totals = rates.totals()
save_rolling_rates(today_totals)
rolling = rolling_rates_summary(load_rolling_rates())

# Build blocks (Wave 3 plan 02-03 also adds the _send_blocks_webhook helper):
blocks = [
    *existing_lead_card_blocks,
    {"type": "divider"},
    build_funnel_block(funnel.pipeline_name, funnel.as_ordered_dict()),
    {"type": "divider"},
    build_service_rates_block(rates.per_run_rates(), rolling),
]
```

## Tests

| File | Test count | Coverage |
|---|---|---|
| `tests/unit/test_observability_counters.py` | 12 | FunnelCounter (ordered, set/increment, late gates, no-preseed, pipeline_name) + ServiceRateTracker (success/failure, single success, unknown-service no-op, all-tracked-present, None for zero total, rate computation, case-insensitive normalisation) |
| `tests/unit/test_observability_rates.py` | 14 | load_rolling_rates (missing → {}, corrupt → {}, well-formed round-trip) + save_rolling_rates (round-trip, same-date replace, prune to 7 days, parent dir creation, multi-service, return value, preserves other services on write) + rolling_rates_summary (aggregates, zero total → None, empty input) + STATE_FILE constant |
| `tests/unit/test_slack_funnel_blocks.py` | 12 | build_funnel_block (shape, order preservation, plain-dict accepted, zero-count rendering, plain dict return) + build_service_rates_block (today + 7-day rendering, None handling, fixed display order, missing service treated as None, rounding to nearest int %, plain dict return) + negative test (no HTTP call during construction) |
| **Total** | **38** | All offline, all `tmp_path`-isolated for file I/O |

Run command:
```bash
python -m pytest tests/unit/test_observability_counters.py tests/unit/test_observability_rates.py tests/unit/test_slack_funnel_blocks.py -v
```

## Deviations from Plan

None — plan executed exactly as written. Each task's behaviour list and action steps were implemented one-for-one. No Rule 1/2/3 auto-fixes were needed; no Rule 4 architectural escalations occurred.

## Verification Results

| Check | Result |
|---|---|
| All 38 unit tests pass | PASS |
| Smoke import of all 9 public symbols (7 from observability + 2 from slack_notifier) | PASS |
| `observability.py` has zero imports from `slack_notifier` (dependency direction) | PASS |
| No real `output/observability/service_rates.json` written during test run | PASS (directory absent post-run) |
| `slack_notifier.py` existing functions byte-identical (zero deletions, zero modifications) | PASS (`git diff` shows only additions) |
| Negative test confirms `requests.post` not called during block construction | PASS |

## Self-Check: PASSED

- File `src/observability.py` → FOUND
- File `src/slack_notifier.py` → FOUND (modified)
- File `tests/unit/test_observability_counters.py` → FOUND
- File `tests/unit/test_observability_rates.py` → FOUND
- File `tests/unit/test_slack_funnel_blocks.py` → FOUND
- Commit `323950a` (RED — observability tests) → FOUND
- Commit `549074d` (GREEN — observability module) → FOUND
- Commit `10e1ef7` (RED — slack block tests) → FOUND
- Commit `3f860d2` (GREEN — slack block builders) → FOUND
