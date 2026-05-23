# SiftStack Doc-Ingest Synthesis

> Single entry point for downstream consumers (`gsd-roadmapper` et al.).
> Produced by `gsd-doc-synthesizer` on 2026-05-23. Mode: `new`.

## Doc counts by type

| Type | Count | Sources |
|---|---|---|
| ADR | 0 | (none) |
| SPEC | 0 | (none) |
| PRD | 0 | (none) |
| DOC | 3 | CLAUDE.md, README.md, docs/foia/huntsville_code_enforcement_request.md |
| UNKNOWN | 0 | (none) |
| **Total** | **3** | All high-confidence, all manifest-override |

## Decisions locked

**0** locked decisions. No ADRs in the ingest set; no LOCKED-decision
entries to record. See `decisions.md` for the summary placeholder.

## Requirements extracted

**0** requirements. No PRDs in the ingest set; no requirement IDs to
emit. See `requirements.md` for the summary placeholder.

## Constraints

**0** constraints. No SPECs in the ingest set; no constraint entries to
emit. Schema-shape information surfaced in DOCs (80-column DataSift
CSV, NoticeData fields, FOIA field-set) is captured in `context.md`
under the relevant topic. See `constraints.md` for the summary
placeholder.

## Context topics (16)

Topics synthesized into `context.md` from the 3 DOC inputs:

1. Document Inventory
2. Platform Overview
3. Data Intake Methods
4. Enrichment Pipeline (10 steps)
5. Alabama Foreclosure Pipeline (Jefferson + Madison)
6. Alabama Probate Pipeline (Jefferson + Madison) — APN newspaper publications
7. Alabama Post-Probate Pipeline (Jefferson — Benchmark Web)
8. Alabama APN Post-Probate Pipeline (Jefferson + Madison + Marshall)
9. Alabama Pre-Probate Pipeline (Jefferson + Madison + Marshall) — obituary-driven
10. Alabama Tax-Delinquent + Tax-Sale Pipeline (Jefferson + Madison)
11. Alabama Code-Violation Pipeline (Jefferson + Madison)
12. Marshall County Distressor Coverage (added 2026-05-12)
13. DataSift.ai (REISift) Integration
14. Courthouse Photo Pipeline (build 1.0.28+)
15. Apify Cloud Deployment
16. Saved Searches + County Configuration / Key Domain Rules / Notice Types
    / Buy Box / API Configuration / REI Skill Library / Huntsville FOIA /
    Output + Architecture Map

## Conflicts

| Bucket | Count |
|---|---|
| BLOCKERS | 0 |
| WARNINGS (competing variants) | 0 |
| INFO (auto-resolved) | 3 |

Auto-resolved entries:
- Precedence applied across 3 DOC-type inputs (default ADR>SPEC>PRD>DOC
  had no effect; per-doc manifest integers governed — CLAUDE.md 0 wins
  over README.md 1 over FOIA 2)
- Documentation-scope divergence — README's market-agnostic Knox/Blount
  TN framing vs CLAUDE.md's live Jefferson/Madison/Marshall AL
  production deployment. CLAUDE.md wins on operational reality;
  README's framing preserved as supplementary context.
- Huntsville code-enforcement FOIA documents a planned future
  capability — both FOIA and CLAUDE.md agree the soft-violation
  adapter is not-yet-built; no contradiction.

Full detail at `/Users/shanismith/Desktop/SiftStack/.planning/INGEST-CONFLICTS.md`.

## Cross-reference cycle detection

Cross-ref graph built from classification `cross_refs`. All cross-refs
in this batch point to source code files (`src/*.py`), the LICENSE,
`.env.example`, the FOIA doc (one-way edge from CLAUDE.md), and three
external Skill Library MD files (`~/Documents/Claude/Projects/REI Skill
Library/*.md`). None of the cross-refs point to other classified docs
in this ingest set in a way that forms a cycle. **0 cycles detected.**

Traversal depth: 1 (CLAUDE.md → FOIA is the only doc-to-doc edge; FOIA
has no back-references to CLAUDE.md or README.md). Well under the
depth-50 cap.

## Per-type intel files (pointers)

- Decisions: `/Users/shanismith/Desktop/SiftStack/.planning/intel/decisions.md` — empty (no ADRs)
- Requirements: `/Users/shanismith/Desktop/SiftStack/.planning/intel/requirements.md` — empty (no PRDs)
- Constraints: `/Users/shanismith/Desktop/SiftStack/.planning/intel/constraints.md` — empty (no SPECs)
- Context: `/Users/shanismith/Desktop/SiftStack/.planning/intel/context.md` — full synthesis of the 3 DOC inputs
- Conflicts report: `/Users/shanismith/Desktop/SiftStack/.planning/INGEST-CONFLICTS.md`

## Supplementary context (not in this ingest set)

The user's prompt noted prior `/gsd-map-codebase` outputs at
`/Users/shanismith/Desktop/SiftStack/.planning/codebase/{STACK,
ARCHITECTURE,STRUCTURE,CONVENTIONS,INTEGRATIONS,TESTING,CONCERNS}.md`.
These were NOT classified and are NOT inputs to this synthesis, but
downstream consumers (`gsd-roadmapper`) may cross-reference them
against the synthesized context for code-truth corroboration.

## Status for downstream

**READY** — no blockers, no competing variants, all conflicts
auto-resolved with rationale. Safe for `gsd-roadmapper` to consume.
