## Conflict Detection Report

### BLOCKERS (0)

No blockers detected. All 3 classifications are high-confidence DOC type
with no LOCKED ADR contradictions and no cross-ref cycles in the doc graph.

### WARNINGS (0)

No competing variants detected. No PRDs were ingested, so there are no
competing-acceptance-criteria conflicts to surface for user resolution.

### INFO (3)

[INFO] Auto-resolved: precedence applied across 3 DOC-type inputs
  Note: All 3 ingested files were typed DOC (no ADR/SPEC/PRD). Default
  precedence ADR > SPEC > PRD > DOC therefore had no effect — per-doc
  manifest-override precedence integers governed instead: CLAUDE.md (0)
  > README.md (1) > huntsville_code_enforcement_request.md (2). Lower
  integer wins on any overlap. No locked decisions exist; all entries
  flow through to context.md keyed by source.
  source: /Users/shanismith/Desktop/SiftStack/.planning/intel/classifications/CLAUDE-c1a8d3f7.json
  source: /Users/shanismith/Desktop/SiftStack/.planning/intel/classifications/README-1ade9def.json
  source: /Users/shanismith/Desktop/SiftStack/.planning/intel/classifications/huntsville-code-enforcement-request-a3f7c2e1.json

[INFO] Auto-resolved: documentation-scope divergence (CLAUDE.md wins on precedence)
  Note: README.md's "What It Does" + "Adapting to Your Market" sections
  describe SiftStack as a market-agnostic Knox/Blount-TN-built platform
  with 7 notice types and a 10-step enrichment pipeline that works
  nationwide. CLAUDE.md documents the live production deployment as
  3 Alabama counties (Jefferson + Madison + Marshall) with 5 distressor
  pipelines, Tier 1/Tier 2 ZIP gates, DataSift orchestrators (Benchmark,
  APN probate, pre-probate, tax-distress, distress-proxy, code-violation),
  Apify cloud deployment, and the 80-column DataSift CSV schema. The
  CLAUDE.md Project Overview block also references "Currently focused on
  Knox and Blount counties, Tennessee" — a historical/foundational
  framing inconsistent with the AL-focused body of the same document.
  Resolution: CLAUDE.md (precedence 0) wins on operational reality.
  README's market-agnostic positioning is preserved as supplementary
  context for the public-distribution / REI Skill Library audience.
  Both viewpoints captured in context.md under "Platform Overview".
  source: /Users/shanismith/Desktop/SiftStack/README.md
  source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md

[INFO] Auto-resolved: Huntsville code-enforcement FOIA documents a planned future capability
  Note: docs/foia/huntsville_code_enforcement_request.md (precedence 2)
  describes a soft-violation data source that does NOT yet flow into the
  pipeline — it is a copy-paste-ready Open Records Act letter to be
  filed with Huntsville Community Development to obtain the data. The
  document includes a post-response adapter integration plan
  (`huntsville_code_violations_api.py` parallel to
  `birmingham_code_enforcement_api.py`) but no adapter exists today.
  CLAUDE.md (precedence 0) corroborates this state: it documents the
  Madison code-enforcement coverage gap, points to the FOIA letter
  by path, and lists the Huntsville soft-violation adapter as
  not-yet-built work. No contradiction — both sources agree the FOIA
  describes future work. Captured in context.md under "Huntsville Code
  Enforcement FOIA Request" and "Alabama Code-Violation Pipeline >
  Coverage gap".
  source: /Users/shanismith/Desktop/SiftStack/docs/foia/huntsville_code_enforcement_request.md
  source: /Users/shanismith/Desktop/SiftStack/CLAUDE.md
