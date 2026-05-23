# Roadmap: SiftStack

## Overview

SiftStack is a mature, operational platform. The v1.0 backbone (5 distressor pipelines × 3 AL counties + APN scraper + DataSift Playwright uploader + Apify deployment + 13-skill REI library) is shipped and running daily. This roadmap is **backward-looking on what's already in production** (collapsed under "v1.0 SHIPPED" for traceability) and **forward-looking on the active v1.1 milestone** — the open work captured in CLAUDE.md "What's NOT done" notes and the high-priority bugs from `.planning/codebase/CONCERNS.md`.

The v1.1 milestone resolves three coupled problems: (1) Apify daily runs are dead-on-arrival from a cold-start crash, (2) several silent bugs are degrading lead quality without anyone noticing, and (3) every pipeline today is invoked manually with per-pipeline Slack output — there's no single daily push of the consolidated funnel. The phases sequence is bug-stabilize → observability → scheduler → coverage gaps → quality verification.

## Milestones

- ✅ **v1.0 Operational Backbone** - Phases shipped as of 2026-05-12 (collapsed below)
- 🚧 **v1.1 Stabilize + Schedule + Close Coverage Gaps** - Phases 1-5 (in progress)
- 📋 **v2.0 Tech Debt + New Coverage + Observability** - Phases TBD (planned; deferred)

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions

- [ ] **Phase 1: Stabilize Production** - Fix the 3 silent bugs + parser gap blocking quality and Apify cold-start
- [ ] **Phase 2: Funnel Transparency** - Per-gate drop counts on every run + per-service success-rate metric on Slack
- [ ] **Phase 3: Unified Daily Scheduler** - One scheduled invocation runs all 5 pipelines + emits one consolidated Slack post
- [ ] **Phase 4: Close Known Coverage Gaps** - Madison probate bonds, pre-probate partner-site fetch, Huntsville FOIA adapter, Marshall tax back-fill
- [ ] **Phase 5: Verify Coverage Quality** - Ground-truth precision check + vacancy enrichment to tighten lead score

## Phase Details

<details>
<summary>✅ v1.0 Operational Backbone (shipped — collapsed)</summary>

v1.0 is the operational baseline as of 2026-05-23. See `.planning/REQUIREMENTS.md` "Validated" section for the full inventory (10 INGEST + 13 ENR + 7 PIPE + 12 OUT + 11 ANL + 4 DEP requirements). High-level shipped scope:

- **Ingestion**: APN scraper (Jefferson + Madison + Marshall foreclosure/probate/pre-probate/code-violation), Benchmark Web Jefferson post-probate, legacy.com obit harvesters (3 markets), property API adapters (Madison + Jefferson + Marshall AssuranceWeb + E-Ring), tax-delinquent adapters (Madison Kendo + Jefferson HTML), Huntsville Unsafe Buildings PDF, Birmingham Accela (5 record types), Hoover SeeClickFix, courthouse photo pipeline (TN legacy).
- **Enrichment**: 10-step pipeline (Filter Sold → Dedup → Vacant → Entity → Probate Property Lookup → Smarty → Zillow → Obituary → Validate → Mailable), probate Tier 1+2 waterfall, pre-probate 3-path search waterfall (~92% Jefferson recall), Tracerfy + Trestle skip-trace, AL intestate-succession DM ranking, Tier 1/Tier 2 ZIP gate across all 3 counties.
- **Pipelines**: 5 distressor orchestrators (Benchmark, APN post-probate, pre-probate, tax-distress, code-violation) + distress-proxy stopgap for Huntsville coverage.
- **Output**: DataSift 80-column CSV + Playwright uploader (login + Add Data wizard + per-distressor split + Enrich + Skip Trace + preset management + Sequence Builder + SiftMap sold tagging), reportlab PDFs, Slack/Discord webhooks, Google Drive upload.
- **Analysis layer**: Comp analyzer (Two-Bucket ARV), rehab estimator (4-tier), deal analyzer (MAO + financing), market analyzer (6-factor zip scoring), buyer + deep prospector, lead manager (4 Pillars + STABM), 26 TCA sequence templates, 12+9 niche/bulk presets, playbook generator, Market Finder extractor.
- **Deployment**: Apify Actor (`apify/actor-python-playwright:3.12` base), 24-field input schema, 15+ CLI modes, REI Skill Library (13 `.skill`/`.plugin` ZIPs).

</details>

### Phase 1: Stabilize Production
**Goal**: Eliminate the 3 silent bugs + 1 parser gap that are degrading lead quality and blocking Apify daily runs. After this phase, production runs are trustworthy.
**Depends on**: Nothing (first phase of active v1.1 milestone)
**Requirements**: BUGFIX-01, BUGFIX-02, BUGFIX-03, PARSER-01
**Success Criteria** (what must be TRUE):
  1. User can deploy to Apify and the scheduled Actor run completes without `AttributeError` (no more `config.TNPN_EMAIL` references; Actor `_cred_map` is current)
  2. Madison probate property-locator returns hits at parity with Jefferson on a ground-truth fixture (`_search_madison` retries with last-first reorder, mirroring `_search_jefferson`)
  3. Records that ship to DataSift always have a real letter-containing address (numeric-only addresses are rejected at validation, not at DataSift cleanup)
  4. AL probate notices with name-first signature blocks land in DataSift with PR mailing addresses populated even when ANTHROPIC_API_KEY is unset (`PR_ADDRESS_NAME_FIRST_RE` matches before falling through to LLM)
  5. A single `pytest tests/unit/` run covers all 4 fixes with golden tests that would have caught the original bug class
**Plans**: TBD

### Phase 2: Funnel Transparency
**Goal**: Make pipeline behavior observable on every run so quality regressions surface immediately, not 24-48h later when "fewer records produced today" gets noticed.
**Depends on**: Phase 1
**Requirements**: OPS-03, OBS-01
**Success Criteria** (what must be TRUE):
  1. Every pipeline run logs and Slacks the full funnel — drop counts at every gate (raw scraped → ZIP-gated → property-matched → obit-matched → tracerfy-matched → DataSift-uploaded)
  2. User can audit any per-run conversion drop visually in the Slack summary without re-running anything
  3. Per-service success-rate metric appears in the daily Slack report for 2Captcha solve rate, Smarty hit rate, Tracerfy match rate, and LLM extraction success rate
  4. When 2Captcha drops from 99% → 80% solve rate (or any service degrades), the daily Slack post surfaces it as a yellow warning before the next run silently breaks
**Plans**: TBD

### Phase 3: Unified Daily Scheduler
**Goal**: Replace per-pipeline manual invocation with a single scheduled daily entry point that runs all 5 pipelines + Marshall coverage and emits one consolidated Slack post for all DataSift leads.
**Depends on**: Phase 2 (funnel data feeds the consolidated post)
**Requirements**: OPS-01, OPS-02
**Success Criteria** (what must be TRUE):
  1. A single Apify Schedule (or local cron equivalent) triggers all 5 distressor pipelines + Marshall coverage in one daily run with no per-pipeline manual `python src/...` invocation needed
  2. One consolidated Slack post per day shows enriched-lead counts grouped by pipeline and county, with action cards for the highest-value leads (currently the user receives N separate posts — they want 1)
  3. The scheduler honors the existing Tier 1/Tier 2 ZIP gates, fiduciary detection, ZIP-recovery Smarty calls, and DataSift list routing without per-pipeline reconfiguration
  4. Pipeline failures (one pipeline crashes) don't abort the rest — the unified post shows what ran, what failed, and the funnel for each
**Plans**: TBD

### Phase 4: Close Known Coverage Gaps
**Goal**: Build the four adapters/back-fills that close documented "What's NOT done" gaps from CLAUDE.md, lifting coverage on the pipelines where it's measurably leaking leads.
**Depends on**: Phase 3 (new adapters wire into the unified scheduler)
**Requirements**: PROB-MAD-01, PREPROB-01, CODE-MAD-01, TAX-MAR-01
**Success Criteria** (what must be TRUE):
  1. Madison post-probate catches PRs that don't formally publish a Notice-to-Creditors via Probate Bonds recordings (`madisonprobate.countygovservices.com` Bonds category) — measurable lift in Madison post-probate volume
  2. Pre-probate recovers the full family graph for funeral-home partner pages (Welch, Larkin & Scott, dignitymemorial) — the ~30-40% obit-drop rate after legacy → obits.al.com upgrade shrinks
  3. Huntsville soft-violations (IPMC, overgrowth, inoperable vehicles, zoning) land in DataSift on a monthly cadence via `huntsville_code_violations_api.py` once City Clerk's monthly FOIA export is flowing — adapter mirrors Birmingham Accela field shape per `docs/foia/huntsville_code_enforcement_request.md`
  4. Marshall tax-delinquent flows into the tax-distress pipeline within one run of Marshall County re-enabling the AssuranceWeb DelinquentParcels listing (no `NotImplementedError`, no silent data loss)
**Plans**: TBD

### Phase 5: Verify Coverage Quality
**Goal**: Move from recall-only to recall + precision measurement, and confirm that "ZIP-gated probate property" actually correlates with "unoccupied inherited home" — the core SiftStack value proposition.
**Depends on**: Phase 4 (new sources must be in funnel before precision measurement)
**Requirements**: ENR-01, PREPROB-02
**Success Criteria** (what must be TRUE):
  1. SiftStack pre-probate output has a measured precision number against the RealSupermarket April 2026 `REISift_Upload_Jefferson_Pre-Probate.csv` ground-truth dump (2,438 records) — comparable to the existing ~92% recall measurement
  2. Inherited-probate records carry a vacancy signal (USPS NCOA hit, water-shutoff signal, or both) when available — `vacancy_confirmed` tag fires on records where vacancy is verifiable
  3. DataSift filter preset can isolate "in-tier + ZIP-gated + DM-attached + vacancy-confirmed" probate leads as the highest-confidence outreach subset
  4. The end-to-end conversion narrative (raw notice → enriched lead → vacancy-confirmed → mailed → response) is observable in DataSift sequence reporting
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 (no decimal insertions yet)

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 0. v1.0 Operational Backbone | v1.0 | shipped | Complete | 2026-05-12 |
| 1. Stabilize Production | v1.1 | 0/TBD | Not started | - |
| 2. Funnel Transparency | v1.1 | 0/TBD | Not started | - |
| 3. Unified Daily Scheduler | v1.1 | 0/TBD | Not started | - |
| 4. Close Known Coverage Gaps | v1.1 | 0/TBD | Not started | - |
| 5. Verify Coverage Quality | v1.1 | 0/TBD | Not started | - |
