---
phase: 02-funnel-transparency
phase_number: 2
gathered: 2026-05-24
status: ready_for_planning
---

# Phase 2: Funnel Transparency — Context

<domain>
## Phase Boundary

**Goal** (from ROADMAP): Make pipeline behavior observable on every run so quality regressions surface immediately, not 24-48h later when "fewer records produced today" gets noticed.

**Requirements in scope:**
- **OPS-03**: Full pipeline funnel transparency on every run — drop counts at every gate (scraped → ZIP-gated → property-matched → obit-matched → tracerfy-matched → uploaded), surfaced in Slack + terminal.
- **OBS-01**: Per-service success-rate metric on Slack daily report (2Captcha solve rate, Smarty hit rate, Tracerfy match rate, LLM extraction success rate) so early-warning surfaces before failed runs.

**Not in scope (deferred):**
- Cross-pipeline rollup into a single consolidated Slack post — that's Phase 3 (Unified Daily Scheduler) territory.
- Long-term metric persistence beyond the rolling 7-day window (e.g., Grafana, structured time-series DB) — v2.
- Alert thresholds / paging rules — Phase 2 surfaces numbers; humans decide when to act.

</domain>

<decisions>
## Implementation Decisions

### Funnel gate scope — per-pipeline, not unified
Each of the 5 pipelines posts its OWN funnel with its OWN gate sequence. Honors each pipeline's actual flow and preserves drop-cause attribution (obit-match failures stay visible for pre-probate; CAPTCHA failures stay visible for APN scraping; etc.).

**Why:** A unified gate set would erase the most informative drop reasons. Per-pipeline gate sets match the existing `_smarty_zip_for_*` / `is_target_county` / Tier-filter / Tracerfy stages already present in each orchestrator.

**How to apply:** Each pipeline's orchestrator defines its own gate sequence + counts. Phase 3 (Unified Daily Scheduler) will roll these up into a single consolidated post with per-pipeline subsections — that consolidation is NOT this phase's job. Today, accept 5 separate Slack posts per scheduled run.

**Pipeline-specific gate sets to instrument:**
| Pipeline | Gate sequence |
|---|---|
| `main.py daily` (APN multi-type) | scraped → seen_ids deduped → county-filtered → parsed → Tier-1/2 ZIP-gated → AL property enriched → Smarty-standardized → Zillow-enriched → Tracerfy-matched → DataSift-uploaded |
| `apn_probate_pipeline_al.py` | scraped → seen_ids deduped → decedent-name searched → Tier-gated → Tracerfy-matched → DataSift-uploaded |
| `pre_probate_pipeline_al.py` | obits harvested → cross-source deduped → fetched → LLM-extracted → DoD-gated → property-matched (3 paths) → Tier-gated → Tracerfy-matched → DataSift-uploaded |
| `tax_distress_pipeline.py` (when invoked daily) | bulk fetched → individual-owner filtered → min-balance filtered → Smarty geocoded (Madison/Marshall) → Tier-gated |
| `code_violation_pipeline.py` (when invoked daily) | bulk fetched (Huntsville PDF + Birmingham Accela + Hoover SeeClickFix) → owner-enriched → Tier-gated |

### Slack format — append funnel block to existing per-run post
The existing per-run Slack post (lead cards) gets a new Block Kit "Funnel" section appended. One message, more content. No threading, no separate posts, no doubled notification volume.

**Why:** Lowest disruption to current Slack consumption pattern. The user already scans these posts daily; the new block adds signal without adding cognitive load of a second message.

**How to apply:** `src/slack_notifier.py` gets a new helper (e.g., `build_funnel_block(funnel: dict) -> Block`) that each pipeline's `notify_slack()` calls and appends to its existing blocks list before `_send_webhook()`.

### Success-rate window — both today's rate AND 7-day rolling baseline
Slack shows e.g. `Smarty: 86% today | 92% 7-day avg`. Best signal for spotting today-vs-baseline regressions; one glance tells you "today is normal" vs "today is degraded".

**Why:** Per-run alone is noisy (one bad run looks like the sky is falling); rolling alone is slow to react (smooths out today's spike). Both side-by-side gives both signals.

**How to apply:**
- Per-run rate is computed in-memory at end-of-run from the pipeline's call counters.
- 7-day rolling rate requires a small JSON state file at `output/observability/service_rates.json` (or `state/service_rates.json` if a state dir convention emerges) that each run appends to and prunes >7-day entries from. Read-modify-write per run; small file, no concurrency concerns (single daily invocation).
- The Slack renderer pulls both values and emits the "today | 7-day" line per service.
- 4 services to track: **2Captcha** (solve rate per attempt), **Smarty** (delivery-line-1 hit rate per call), **Tracerfy** (match rate per contact submitted), **LLM** (extraction-success rate per call — defined as "structured fields returned" not "HTTP 200").

### Claude's discretion (not separately discussed — Claude decides defaults)
- **Alert thresholds**: No alerting in Phase 2. Just emit numbers. Humans decide what's bad.
- **State file location**: `output/observability/service_rates.json` (under `output/` which is already gitignored).
- **State file format**: One JSON object per service, array of `{date, success, total}` entries, pruned to last 7.
- **Terminal output**: Same funnel data printed to terminal at end-of-run via the existing `logging.info` pattern. No separate terminal formatter — re-use the structured funnel dict that Slack receives.
- **Failure semantics for each service**:
  - 2Captcha: success = solved on any of the 3 attempts; failure = exhausted retries (`attempt 3/3` followed by no "solved" line).
  - Smarty: success = response has at least one `delivery_line_1`; failure = empty array OR HTTP error.
  - Tracerfy: success = batch endpoint returns at least one contact with `matched: true`; failure = matched=false OR HTTP error.
  - LLM: success = response JSON parses AND has the expected top-level keys (e.g., `decedent_full_name` for obit extraction); failure = JSON parse error OR missing required keys OR HTTP error.

</decisions>

<specifics>
## Specific Code Touchpoints

- `src/slack_notifier.py` — `_send_webhook()` is the single send path. Add `build_funnel_block()` + `build_service_rates_block()` helpers here.
- Each pipeline's `notify_slack()` function — wires funnel + service-rate blocks into its existing block list:
  - `src/main.py` (legacy daily — has a Slack post at end of `run_full_pipeline`)
  - `src/apn_probate_pipeline_al.py:notify_slack()`
  - `src/pre_probate_pipeline_al.py:notify_slack()`
  - `src/benchmark_pipeline_al.notify_slack()`
  - `src/tax_distress_pipeline.py` (when Phase 3 daily scheduler invokes it)
  - `src/code_violation_pipeline.py` (when Phase 3 daily scheduler invokes it)
- New module: `src/observability.py` — holds the `FunnelCounter` class + `ServiceRateTracker` class + `load_rolling_rates()` / `save_rolling_rates()` helpers + the JSON state-file path constant.
- New state file: `output/observability/service_rates.json` (per-day per-service success/total counts, pruned to 7 entries).
- Per-service call sites that need to increment success/failure counters:
  - `src/captcha_solver.py` — at end of solve attempt
  - `src/address_standardizer.py:standardize_addresses()` — per Smarty response
  - `src/tracerfy_skip_tracer.py:batch_skip_trace()` — per response (success rate is `matched / total_submitted`)
  - `src/llm_parser.py` and `src/pre_probate_pipeline_al.py:_extract_decedent_via_llm()` — per LLM call

</specifics>

<canonical_refs>
## Canonical References

- `.planning/ROADMAP.md` — Phase 2 goal + 4 success criteria (the contract this phase delivers against)
- `.planning/REQUIREMENTS.md` — OPS-03 + OBS-01 acceptance text
- `.planning/PROJECT.md` — project framing + tier ZIP definitions
- `.planning/codebase/ARCHITECTURE.md` — current Slack notifier pattern + per-pipeline orchestrator layout
- `.planning/codebase/INTEGRATIONS.md` — current service-call sites (2Captcha, Smarty, Tracerfy, LLM)
- `CLAUDE.md` — DataSift.ai Slack notifier pattern + "Realistic conversion rates" baselines per pipeline (useful as sanity bounds for the rolling rates)

User-memory anchor (informs the user-facing posture, not the code): **"Always share the entire pipeline funnel after a run — drop counts at every gate so the user can audit conversion vs assume a break."** Phase 2 operationalizes this preference.

</canonical_refs>

<deferred>
## Deferred Ideas

- **Cross-pipeline rolled-up Slack post** — Phase 3 (Unified Daily Scheduler) consolidates the 5 per-pipeline posts into one. Per-pipeline subsections inside that post.
- **Alert thresholds + paging** — Phase 5 or later. Once we have rolling baselines, we can add yellow/red bands. Not in Phase 2.
- **Time-series persistence** — Grafana / SQLite / Prometheus exposition. v2 territory.
- **Symmetric `owner_street` garbage validation** (carried over from Phase 1's BUGFIX-03 skip note) — not Phase 2's scope; track as a Phase 5+ or v2 item.

</deferred>
