"""Daily-sweep finalizer — uploads every DataSift-formatted CSV produced
during this run AND posts a consolidated Slack summary with per-pipeline
funnels + per-CSV upload outcomes.

Invoked from .github/workflows/daily-sweep.yml after all three pipelines
(main.py daily, apn_probate, pre_probate) have run. Relies on:

  * ``output/.finalize_start`` marker — touched by the workflow before any
    pipeline runs. Any DataSift CSV newer than this belongs to this sweep.

  * ``logs/daily_main_*.log`` / ``logs/daily_apn_*.log`` / ``logs/daily_pre_*.log``
    — each pipeline step tees its stdout/stderr to one of these via the
    workflow. The finalizer parses them for funnel numbers (scraped count,
    drop reasons, Tracerfy match rate, Trestle scoring stats, etc.).

Exits non-zero if any upload fails, so the workflow's failure-notification
step surfaces it. Slack message is sent in all cases — including the "no
CSVs produced" path — so the operator never has to guess whether a run
silently dropped.
"""

import asyncio
import csv
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

from datasift_uploader import upload_to_datasift_per_distressor  # noqa: E402
from datasift_formatter import HEIRS_DISPLAY_LABELS  # noqa: E402
from slack_notifier import _send_webhook  # noqa: E402

OUT = REPO / "output"
LOGS = REPO / "logs"
RUN_MARKER = OUT / ".finalize_start"


# ── Log parsers ─────────────────────────────────────────────────────────

@dataclass
class MainDailyFunnel:
    saved_searches: int = 0
    scraped: int = 0
    tier_input: int = 0
    tier_output: int = 0
    tier_off: int = 0
    tier_no_zip: int = 0
    entity_dropped: int = 0
    probate_found: int = 0
    probate_not_found: int = 0
    probate_skipped: int = 0
    probate_total: int = 0
    smarty_matched: int = 0
    smarty_failed: int = 0
    zillow_enriched: int = 0
    zillow_failed: int = 0
    trestle_scored: int = 0
    trestle_records: int = 0
    obit_confirmed: int = 0
    obit_total: int = 0
    tracerfy_matched: int = 0
    tracerfy_total: int = 0
    tracerfy_phones: int = 0
    tracerfy_emails: int = 0
    tracerfy_cost: float = 0.0
    dms_count: int = 0
    heirs_count: int = 0


@dataclass
class ApnProbateFunnel:
    scraped: int = 0
    enriched: int = 0
    tier1: int = 0
    tier2: int = 0
    dropped_off_tier: int = 0
    dropped_no_property: int = 0
    dedupes: int = 0


@dataclass
class PreProbateFunnel:
    harvested: int = 0
    enriched: int = 0
    tier1: int = 0
    tier2: int = 0
    dropped_off_tier: int = 0
    dropped_no_property: int = 0
    dropped_not_obituary: int = 0
    dropped_stale: int = 0
    dropped_dupe: int = 0
    dropped_fetch_fail: int = 0
    trace_submitted: int = 0
    trace_matched: int = 0
    trace_phones: int = 0
    trace_emails: int = 0
    trace_cost: float = 0.0


@dataclass
class CodeViolationFunnel:
    """Funnel state for the code_violation pipeline (Huntsville +
    Birmingham + Hoover). Distinct from the others because CV's
    cross-run dedup (seen_code_violations.json, 180-day prune window)
    is a first-class filter — the operator needs to see both fresh and
    skipped counts to understand why "daily new" volume is small."""
    bulk_fetched: int = 0
    owner_enriched: int = 0
    tier_gated: int = 0
    dedup_fresh: int = 0        # Passed cross-run dedup — actually emitted
    dedup_skipped: int = 0      # Seen within 180-day prune window — skipped
    dedup_no_case: int = 0      # No case# — passed through (can't dedup)
    tracerfy_submitted: int = 0
    tracerfy_matched: int = 0
    tracerfy_phones: int = 0
    tracerfy_emails: int = 0
    tracerfy_cost: float = 0.0
    csv_written: int = 0        # Final row count in the datasift CSV


def _first_int(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def _last_match(pattern: str, text: str) -> tuple | None:
    matches = list(re.finditer(pattern, text))
    return matches[-1].groups() if matches else None


def parse_main_daily(path: Path) -> MainDailyFunnel:
    if not path.exists():
        return MainDailyFunnel()
    text = path.read_text(encoding="utf-8", errors="replace")
    f = MainDailyFunnel()

    f.saved_searches = _first_int(r"Running (\d+) saved searches", text)
    f.scraped = _first_int(r"Total notices scraped:\s*(\d+)", text)

    m = re.search(
        r"Tier filter.*?:\s*(\d+) → (\d+) \(dropped (\d+) off-tier,\s*(\d+) no-ZIP\)",
        text,
    )
    if m:
        f.tier_input, f.tier_output, f.tier_off, f.tier_no_zip = (
            int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        )

    f.entity_dropped = _first_int(r"Removed (\d+) entity-owned records", text)

    m = re.search(
        r"Property lookup complete:\s*(\d+) found,\s*(\d+) not found,\s*(\d+) skipped \(of (\d+) total\)",
        text,
    )
    if m:
        f.probate_found = int(m.group(1))
        f.probate_not_found = int(m.group(2))
        f.probate_skipped = int(m.group(3))
        f.probate_total = int(m.group(4))

    m = re.search(
        r"Smarty.*?complete:\s*(\d+) matched,\s*(\d+) failed", text
    )
    if m:
        f.smarty_matched, f.smarty_failed = int(m.group(1)), int(m.group(2))

    m = re.search(
        r"Zillow enrichment complete:\s*(\d+) enriched,\s*(\d+) failed", text
    )
    if m:
        f.zillow_enriched, f.zillow_failed = int(m.group(1)), int(m.group(2))

    m = re.search(
        r"Trestle scored (\d+) unique phones across (\d+) records", text
    )
    if m:
        f.trestle_scored, f.trestle_records = int(m.group(1)), int(m.group(2))

    # Phase A — use the LAST progress marker (final state)
    last_phase = _last_match(
        r"Phase A progress:\s*(\d+)/(\d+) \(confirmed=(\d+)", text
    )
    if last_phase:
        f.obit_total = int(last_phase[1])
        f.obit_confirmed = int(last_phase[2])

    m = re.search(
        r"Tracerfy batch complete:\s*(\d+)/(\d+) matched,\s*(\d+) phones,\s*(\d+) emails,\s*\$([\d.]+)",
        text,
    )
    if m:
        f.tracerfy_matched = int(m.group(1))
        f.tracerfy_total = int(m.group(2))
        f.tracerfy_phones = int(m.group(3))
        f.tracerfy_emails = int(m.group(4))
        f.tracerfy_cost = float(m.group(5))

    f.dms_count = _first_int(r"DMs CSV:\s*(\d+) records", text)
    f.heirs_count = _first_int(r"Heirs CSV:\s*(\d+) records", text)
    return f


def parse_apn_probate(path: Path) -> ApnProbateFunnel:
    if not path.exists():
        return ApnProbateFunnel()
    text = path.read_text(encoding="utf-8", errors="replace")
    f = ApnProbateFunnel()

    f.scraped = _first_int(r"Total notices scraped:\s*(\d+)", text)
    f.enriched = _first_int(r"enriched \(in target ZIP\):\s*(\d+)", text)
    f.tier1 = _first_int(r"Tier 1:\s*(\d+)", text)
    f.tier2 = _first_int(r"Tier 2:\s*(\d+)", text)
    f.dropped_off_tier = _first_int(r"dropped \(off-target ZIP\):\s*(\d+)", text)
    f.dropped_no_property = _first_int(r"dropped \(no property\):\s*(\d+)", text)
    f.dedupes = len(re.findall(r"SKIP.*dup decedent", text))
    return f


def parse_code_violation(path: Path) -> CodeViolationFunnel:
    """Parse the code_violation pipeline's log (logs/daily_code_*.log).

    Log line formats we extract from:
      - "owner_enriched gate: 419 notices have an owner_name" — owner count
      - "Cross-run dedup: 217 → 8 (skipped 209 already-seen, 0 had no case# ...)" — dedup
      - "Funnel (code_violation): {'bulk_fetched': 469, ...}" — full funnel dict
      - "Skip-trace stats: submitted=X matched=Y phones=Z emails=W cost=$..."
      - "Wrote 8 records to DataSift CSV: ..." — final CSV size
    """
    if not path.exists():
        return CodeViolationFunnel()
    text = path.read_text(encoding="utf-8", errors="replace")
    f = CodeViolationFunnel()

    # Funnel dict — pulls bulk_fetched + owner_enriched + tier_gated in one shot
    m = re.search(
        r"Funnel \(code_violation\):\s*\{[^}]*"
        r"'bulk_fetched':\s*(\d+)[^}]*"
        r"'owner_enriched':\s*(\d+)[^}]*"
        r"'tier_gated':\s*(\d+)",
        text,
    )
    if m:
        f.bulk_fetched = int(m.group(1))
        f.owner_enriched = int(m.group(2))
        f.tier_gated = int(m.group(3))

    # Cross-run dedup line
    m = re.search(
        r"Cross-run dedup:\s*(\d+)\s*→\s*(\d+)\s*"
        r"\(skipped\s+(\d+)\s+already-seen,\s*(\d+)\s+had no case",
        text,
    )
    if m:
        # group(1) = tier_gated pre-dedup, group(2) = fresh, group(3) = skipped, group(4) = no-case
        f.dedup_fresh = int(m.group(2))
        f.dedup_skipped = int(m.group(3))
        f.dedup_no_case = int(m.group(4))

    # Skip-trace stats
    m = re.search(
        r"Skip-trace stats:\s*submitted=(\d+)\s+matched=(\d+)\s+"
        r"phones=(\d+)\s+emails=(\d+)\s+cost=\$([\d.]+)",
        text,
    )
    if m:
        f.tracerfy_submitted = int(m.group(1))
        f.tracerfy_matched = int(m.group(2))
        f.tracerfy_phones = int(m.group(3))
        f.tracerfy_emails = int(m.group(4))
        f.tracerfy_cost = float(m.group(5))

    # CSV row count from the "Wrote N records to DataSift CSV" line
    f.csv_written = _first_int(r"Wrote (\d+) records? to DataSift CSV", text)
    return f


def parse_pre_probate(path: Path) -> PreProbateFunnel:
    if not path.exists():
        return PreProbateFunnel()
    text = path.read_text(encoding="utf-8", errors="replace")
    f = PreProbateFunnel()

    f.harvested = _first_int(r"Harvested (\d+) obituary URL", text)
    f.enriched = _first_int(r"enriched \(in target ZIP\):\s*(\d+)", text)
    f.tier1 = _first_int(r"Tier 1:\s*(\d+)", text)
    f.tier2 = _first_int(r"Tier 2:\s*(\d+)", text)
    f.dropped_off_tier = _first_int(r"dropped \(off-target ZIP\):\s*(\d+)", text)
    f.dropped_no_property = _first_int(r"dropped \(no AL property\):\s*(\d+)", text)
    f.dropped_not_obituary = _first_int(r"dropped \(not obituary\):\s*(\d+)", text)
    f.dropped_stale = _first_int(r"dropped \(stale DoD\):\s*(\d+)", text)
    f.dropped_dupe = _first_int(r"dropped \(duplicate\):\s*(\d+)", text)
    f.dropped_fetch_fail = _first_int(r"dropped \(fetch failed\):\s*(\d+)", text)

    m = re.search(
        r"Skip-trace stats:\s*submitted=(\d+)\s+matched=(\d+)\s+phones=(\d+)\s+emails=(\d+)\s+cost=\$([\d.]+)",
        text,
    )
    if m:
        f.trace_submitted = int(m.group(1))
        f.trace_matched = int(m.group(2))
        f.trace_phones = int(m.group(3))
        f.trace_emails = int(m.group(4))
        f.trace_cost = float(m.group(5))
    return f


# ── CSV discovery + categorization ──────────────────────────────────────

def _csvs_from_this_run(after_ts: float) -> list[Path]:
    """Find all datasift upload CSVs written this run, sorted by upload
    priority (lowest priority value first → uploads first), with any
    multi-file distressors consolidated into a single upload CSV.

    Consolidation (2026-06-11 operator request): main.py daily writes a
    probate file AND apn_probate may write a SECOND probate file. Both go
    to the same DataSift list, so we concat them into one upload file
    before submitting — avoids a redundant browser session, redundant
    enrich pass, and confusing audit trail. Both original files are kept
    on disk for archive purposes; only the consolidated file gets uploaded.

    Skips:
      * datasift_archive_*.csv — audit only, never uploaded
      * Legacy DMs/Heirs masters — old build artifacts that would
        duplicate the new per-distressor files

    Priority comes from DISTRESSOR_PRIORITY in datasift_formatter via
    the row 1 Notice Type. Unknown types sort last (priority 99)."""
    from datasift_formatter import distressor_sort_key

    candidates: list[Path] = []
    for root in (OUT / "leads", OUT):
        if root.exists():
            candidates.extend(root.glob("datasift_upload*.csv"))

    # Dedup + freshness filter
    seen: set[Path] = set()
    fresh: list[Path] = []
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if p.stat().st_mtime <= after_ts:
            continue
        if "_DMs_" in p.name or "_Heirs_" in p.name:
            continue  # legacy master files — see docstring
        # Skip already-consolidated files from a prior partial run — they'd
        # be re-detected by the freshness filter but we don't want them as
        # inputs to a fresh consolidation pass.
        if "_consolidated_" in p.name:
            continue
        fresh.append(p)

    # Group by row 1 Notice Type, consolidate multi-file groups.
    grouped: dict[str, list[Path]] = {}
    for p in fresh:
        nt = _notice_type_from_csv(p)
        grouped.setdefault(nt, []).append(p)

    uploads: list[Path] = []
    for nt, group in grouped.items():
        if len(group) == 1:
            uploads.append(group[0])
        else:
            consolidated = _consolidate_csvs(group, nt)
            if consolidated:
                uploads.append(consolidated)
                logger = __import__("logging").getLogger(__name__)
                logger.info(
                    "Consolidated %d %s files → %s",
                    len(group), nt or "unknown", consolidated.name,
                )

    # Sort by upload priority via each CSV's row 1 Notice Type.
    def _priority_for(p: Path) -> tuple[int, str]:
        nt = _notice_type_from_csv(p)
        return (distressor_sort_key(nt), p.name)

    return sorted(uploads, key=_priority_for)


def _consolidate_csvs(group: list[Path], notice_type: str) -> Path | None:
    """Concat multiple CSVs of the same notice_type into one upload file.

    Operator request 2026-06-11: when main.py daily AND apn_probate both
    produce probate files on the same day, merge them so DataSift sees ONE
    consolidated upload (one browser session, one enrich pass, one Slack
    line). The originals stay on disk for archive — only the consolidated
    file is uploaded.

    Filename: datasift_upload_<notice_type>_consolidated_<ts>.csv. Header
    row comes from group[0]; subsequent files' header rows are skipped.
    Returns the consolidated file path, or None if the group was empty or
    all files were unreadable.
    """
    from datetime import datetime as _dt

    if not group:
        return None

    nt_slug = notice_type or "unknown"
    timestamp = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = group[0].parent
    out_path = out_dir / f"datasift_upload_{nt_slug}_consolidated_{timestamp}.csv"

    # Two-pass: collect the UNION of all input fieldnames first, then write
    # rows with that union as the output header.
    #
    # Single-pass with "header = group[0]'s fieldnames" silently dropped
    # all columns from later files that weren't in the first file. Caused
    # the 2026-06-28 regression where pre_probate's Sunday obit-refresh
    # CSV (14 columns) got picked as group[0], stripping 66 columns off
    # every row from the regular pre_probate CSV that followed it in the
    # group. Net effect: rich-data rows became degraded "address + obit-
    # refresh notes" rows that wiped Mailing + Notes via swap-mode upload.
    #
    # The union approach preserves rich data while letting minimal-schema
    # CSVs like the refresh delta coexist — missing columns ship as empty
    # for those rows, which under swap mode means "no change" for those
    # fields (CSV writer omits absent keys, swap-mode preserves untouched
    # columns).
    union: list[str] = []
    seen_cols: set[str] = set()
    valid_sources: list[Path] = []
    for src in group:
        try:
            with open(src, newline="", encoding="utf-8") as in_f:
                reader = csv.DictReader(in_f)
                for col in (reader.fieldnames or []):
                    if col not in seen_cols:
                        seen_cols.add(col)
                        union.append(col)
                valid_sources.append(src)
        except Exception as e:
            logger = __import__("logging").getLogger(__name__)
            logger.warning("Skipping %s during consolidation: %s", src, e)
            continue

    if not union or not valid_sources:
        return None

    rows_written = 0
    with open(out_path, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(
            out_f, fieldnames=union, extrasaction="ignore",
        )
        writer.writeheader()
        for src in valid_sources:
            try:
                with open(src, newline="", encoding="utf-8") as in_f:
                    reader = csv.DictReader(in_f)
                    for row in reader:
                        writer.writerow(row)
                        rows_written += 1
            except Exception as e:
                logger = __import__("logging").getLogger(__name__)
                logger.warning("Skipping %s during consolidation pass 2: %s", src, e)
                continue

    if rows_written == 0:
        try:
            out_path.unlink()
        except OSError:
            pass
        return None
    return out_path


def _notice_type_from_csv(p: Path) -> str:
    """Read row 1's Notice Type. Empty string if unreadable."""
    try:
        with open(p, newline="", encoding="utf-8") as f:
            row = next(csv.DictReader(f), None)
        if row:
            return (row.get("Notice Type") or "").strip()
    except Exception:
        pass
    return ""


def _categorize(p: Path) -> str:
    """Map a CSV to its log-parsing bucket (for funnel display).

    Filename hints come first (some pipelines stamp the distressor in the
    name), then row 1 Notice Type as fallback. The buckets here are
    log/funnel labels — NOT DataSift list names — so probate vs
    apn_probate is a meaningful distinction (different log sources)."""
    if "code_violation" in p.name:
        return "code_violation"
    nt = _notice_type_from_csv(p)
    if nt == "pre_probate":
        return "pre_probate"
    if nt == "probate":
        return "apn_probate"
    if nt == "code_violation":
        return "code_violation"
    if nt == "foreclosure":
        return "main_dms"  # main.py daily foreclosure output
    return "unknown"


def _newest_log(pattern: str) -> Path | None:
    if not LOGS.exists():
        return None
    matches = sorted(LOGS.glob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


# ── Upload + Slack ──────────────────────────────────────────────────────

async def _upload_one(
    p: Path,
    label: str,
    *,
    mode: str = "auto",
    enrich: bool = True,
    skip_trace: bool = True,
) -> dict:
    """Upload one CSV through one wizard pass.

    Two-pass architecture (operator-clarified 2026-06-29):
      Pass 1 (mode="add", enrich=True, skip_trace=True):
        - Called for ALL CSVs. Creates new records + updates existing by
          address. Runs DataSift's Enrich Data + Skip Trace once after.
      Pass 2 (mode="swap", enrich=False, skip_trace=False):
        - Called for swap-eligible CSVs (probate, pre_probate) AFTER
          Pass 1 completes for everyone. Updates owner/contact only;
          enrich/skip-trace skipped since Pass 1 already ran them.
    """
    print(
        f"\n=== Uploading {p.name} ({label}, mode={mode}) ===",
        flush=True,
    )
    try:
        r = await upload_to_datasift_per_distressor(
            p,
            headless=True,
            enrich=enrich,
            skip_trace=skip_trace,
            mode=mode,
        )
    except Exception as e:
        r = {"success": False, "message": f"exception: {e}", "uploads": []}
    r["csv"] = p.name
    r["label"] = label
    r.setdefault("mode", mode)
    print(
        f"{p.name}: mode={r.get('mode')} success={r.get('success')} "
        f"{r.get('message','')}",
        flush=True,
    )
    return r


def _format_main_funnel(f: MainDailyFunnel) -> list[str]:
    if not (f.scraped or f.dms_count or f.heirs_count):
        return ["  (no main.py daily output)"]
    lines = [
        f"  raw-scraped: {f.scraped}  ·  saved-searches: {f.saved_searches}",
    ]
    if f.tier_input or f.tier_output:
        lines.append(
            f"  tier filter: {f.tier_input} → {f.tier_output}  ·  "
            f"off-tier: {f.tier_off}  ·  no-zip: {f.tier_no_zip}"
        )
    if f.entity_dropped:
        lines.append(f"  entity-owned dropped: {f.entity_dropped}")
    if f.probate_total:
        lines.append(
            f"  probate property-locator: matched {f.probate_found}  ·  "
            f"no-property {f.probate_not_found}  ·  skipped {f.probate_skipped}  "
            f"(of {f.probate_total})"
        )
    if f.smarty_matched or f.smarty_failed:
        lines.append(
            f"  Smarty USPS: {f.smarty_matched}/{f.smarty_matched + f.smarty_failed} matched"
        )
    lines.append(
        f"  Zillow: {f.zillow_enriched}/{f.zillow_enriched + f.zillow_failed} enriched"
    )
    if f.obit_total:
        lines.append(
            f"  Obit Phase A: {f.obit_confirmed} confirmed deceased of {f.obit_total} checked"
        )
    if f.tracerfy_total:
        lines.append(
            f"  Tracerfy: {f.tracerfy_matched}/{f.tracerfy_total} matched  ·  "
            f"{f.tracerfy_phones} phones  ·  {f.tracerfy_emails} emails  ·  "
            f"${f.tracerfy_cost:.2f}"
        )
    if f.trestle_records:
        lines.append(
            f"  Trestle: scored {f.trestle_scored} phones across {f.trestle_records} records"
        )
    lines.append(f"  DMs: {f.dms_count} records  ·  Heirs: {f.heirs_count} records")
    return lines


def _format_apn_funnel(f: ApnProbateFunnel) -> list[str]:
    if not (f.scraped or f.enriched):
        return ["  (no apn_probate output — typically 0 when main.py already consumed today's probates via seen_ids)"]
    return [
        f"  scraped: {f.scraped}  ·  in-tier kept: {f.enriched} "
        f"(T1: {f.tier1}, T2: {f.tier2})  ·  off-tier: {f.dropped_off_tier}  ·  "
        f"no-property: {f.dropped_no_property}  ·  same-person dedupes: {f.dedupes}",
    ]


def _format_code_violation_funnel(f: CodeViolationFunnel) -> list[str]:
    """Render the Code Violation section for the Slack summary.

    Makes the 180-day cross-run dedup visible — the operator needs to
    see BOTH the fresh count and the skipped count to understand why
    daily new-volume is small (~0-15/day) despite the source portals
    listing hundreds of active cases at any time.
    """
    if not (f.bulk_fetched or f.dedup_fresh or f.csv_written):
        return ["  (no code_violation output)"]

    lines = [
        f"  bulk-fetched: {f.bulk_fetched}  ·  "
        f"owner-enriched: {f.owner_enriched}  ·  "
        f"tier-gated: {f.tier_gated}",
    ]
    if f.dedup_skipped or f.dedup_fresh:
        no_case_note = (
            f"  ·  {f.dedup_no_case} passed through (no case#)"
            if f.dedup_no_case else ""
        )
        lines.append(
            f"  cross-run dedup: {f.dedup_fresh} fresh  ·  "
            f"{f.dedup_skipped} skipped (seen ≤ 180d){no_case_note}"
        )
    if f.tracerfy_submitted:
        lines.append(
            f"  Tracerfy: {f.tracerfy_matched}/{f.tracerfy_submitted} matched  ·  "
            f"{f.tracerfy_phones} phones  ·  {f.tracerfy_emails} emails  ·  "
            f"${f.tracerfy_cost:.2f}"
        )
    return lines


def _format_pre_funnel(f: PreProbateFunnel) -> list[str]:
    if not f.harvested:
        return ["  (no pre_probate output — Firecrawl listing returned empty)"]
    lines = [
        f"  harvested: {f.harvested}  ·  in-tier: {f.enriched} "
        f"(T1: {f.tier1}, T2: {f.tier2})",
        f"  dropped — off-tier: {f.dropped_off_tier}  ·  no-property: {f.dropped_no_property}  ·  "
        f"not-obit: {f.dropped_not_obituary}  ·  stale-DoD: {f.dropped_stale}  ·  "
        f"dupes: {f.dropped_dupe}  ·  fetch-fail: {f.dropped_fetch_fail}",
    ]
    if f.trace_submitted:
        lines.append(
            f"  Tracerfy: {f.trace_matched}/{f.trace_submitted} matched  ·  "
            f"{f.trace_phones} phones  ·  {f.trace_emails} emails  ·  "
            f"${f.trace_cost:.2f}"
        )
    return lines


# ── Foreclosure source + county breakdown + upcoming auction callouts ──

def _foreclosure_source_label(csv_name: str) -> str | None:
    """Map a foreclosure CSV filename to a human-readable source label.

    Returns None for non-foreclosure CSVs. Filename patterns:
      datasift_upload_foreclosure_<ts>.csv                       → APN (main.py daily)
      datasift_upload_foreclosure_rl_<ts>.csv                    → Rubin Lublin
      datasift_upload_foreclosure_tb_<ts>.csv                    → Tiffany & Bosco (pending)
      datasift_upload_foreclosure_tb_cancelled_<ts>.csv          → T&B Sales Results (Cancelled)
      datasift_upload_foreclosure_tb_postponed_<ts>.csv          → T&B Sales Results (Postponed)
      datasift_upload_foreclosure_hwm_<ts>.csv                   → Halliday Watkins Mann
      datasift_upload_foreclosure_consolidated_<ts>.csv          → SKIP (aggregate)
    """
    if "foreclosure" not in csv_name:
        return None
    if "consolidated" in csv_name:
        return None  # aggregate — its total is the sum of per-source lines
    # Check specific-suffix patterns before the generic _tb_ so cancelled/
    # postponed are distinguished from the pending T&B adapter.
    if "_tb_cancelled_" in csv_name:
        return "T&B Sales Results (Cancelled)"
    if "_tb_postponed_" in csv_name:
        return "T&B Sales Results (Postponed)"
    if "_rl_" in csv_name:
        return "Rubin Lublin"
    if "_tb_" in csv_name:
        return "Tiffany & Bosco (pending)"
    if "_hwm_" in csv_name:
        return "Halliday Watkins Mann"
    return "APN (main.py daily)"


def _analyze_foreclosure_csvs(csvs: list[Path]) -> tuple[list[str], list[str]]:
    """Build the foreclosure source breakdown + upcoming auctions block.

    Returns (breakdown_lines, upcoming_lines). Both are Slack-formatted
    string lists. Breakdown groups records by source + county; upcoming
    lists auctions by 7/14/21-day windows.
    """
    # Per-source county tallies + auction accumulator
    by_source: dict[str, dict[str, int]] = {}
    grand_total: dict[str, int] = {}
    auctions: list[tuple[date, str, str, str]] = []  # (auction_date, address, city, county)
    today = date.today()

    for p in csvs:
        source = _foreclosure_source_label(p.name)
        if source is None:
            continue
        try:
            with open(p, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    county = (row.get("County") or "").strip() or "(unknown)"
                    # Split into 2 steps because Python evaluates the RHS
                    # before the LHS assignment — a single-line chained
                    # setdefault would KeyError on the first row.
                    sub = by_source.setdefault(source, {})
                    sub[county] = sub.get(county, 0) + 1
                    grand_total[county] = grand_total.get(county, 0) + 1

                    # Parse the auction date for the upcoming-auctions block.
                    raw = (row.get("Foreclosure Date") or "").strip()
                    if not raw:
                        continue
                    try:
                        auction_dt = datetime.strptime(raw, "%m/%d/%Y").date()
                    except ValueError:
                        continue
                    if auction_dt < today:
                        continue
                    address = (row.get("Property Street Address") or "").strip()
                    city = (row.get("Property City") or "").strip()
                    auctions.append((auction_dt, address, city, county))
        except Exception:
            continue

    # Breakdown section
    breakdown: list[str] = []
    if by_source:
        breakdown.append("*Foreclosure Sources Breakdown*")
        # Preferred source order — APN first, then trustee portals,
        # historical T&B Results at the end (backfill, not fresh volume)
        source_order = [
            "APN (main.py daily)",
            "Rubin Lublin",
            "Tiffany & Bosco (pending)",
            "Halliday Watkins Mann",
            "T&B Sales Results (Cancelled)",
            "T&B Sales Results (Postponed)",
        ]
        ordered = [s for s in source_order if s in by_source]
        ordered += [s for s in by_source if s not in ordered]
        for src in ordered:
            counties = by_source[src]
            total = sum(counties.values())
            county_str = " | ".join(
                f"{c[:3]} {n}" for c, n in sorted(counties.items())
            )
            breakdown.append(f"  {src}: {total}  ·  {county_str}")
        # Consolidated total (post-dedup)
        grand = sum(grand_total.values())
        grand_str = " | ".join(
            f"{c[:3]} {n}" for c, n in sorted(grand_total.items())
        )
        breakdown.append(f"  *Total (pre-dedup): {grand}*  ·  {grand_str}")

    # Upcoming auctions section — 3 windows, capped rows per window
    upcoming: list[str] = []
    if auctions:
        auctions.sort(key=lambda t: t[0])
        _MAX_ROWS = 8  # cap per window to keep Slack readable
        for label, days in [
            (":rotating_light: *Upcoming Auctions — next 7 days*", 7),
            (":warning: *Upcoming Auctions — 8-14 days*", 14),
            (":clock3: *Upcoming Auctions — 15-21 days*", 21),
        ]:
            # Window boundaries: lower = previous window end + 1, upper = days
            lower = 0 if days == 7 else (7 if days == 14 else 14)
            in_window = [
                a for a in auctions
                if lower < (a[0] - today).days <= days
                or (days == 7 and (a[0] - today).days <= 7 and (a[0] - today).days >= 0)
            ]
            # Deduplicate: same address twice from different sources → one line
            seen_addr: set[str] = set()
            dedup: list[tuple[date, str, str, str]] = []
            for a in in_window:
                key = (a[1] or "").upper()
                if key and key in seen_addr:
                    continue
                if key:
                    seen_addr.add(key)
                dedup.append(a)
            if not dedup:
                continue
            upcoming.append(label + f"  ({len(dedup)})")
            for auction_dt, address, city, county in dedup[:_MAX_ROWS]:
                dleft = (auction_dt - today).days
                city_str = f", {city}" if city else ""
                upcoming.append(
                    f"  • {address}{city_str} ({county}) — "
                    f"{auction_dt.strftime('%Y-%m-%d')} ({dleft}d)"
                )
            if len(dedup) > _MAX_ROWS:
                upcoming.append(f"  _...and {len(dedup) - _MAX_ROWS} more_")

    return breakdown, upcoming


def _build_slack_message(
    main_f: MainDailyFunnel,
    apn_f: ApnProbateFunnel,
    pre_f: PreProbateFunnel,
    results: list[dict],
    csv_count: int,
    dropbox_results: list[dict] | None = None,
    csvs: list[Path] | None = None,
    code_violation_f: CodeViolationFunnel | None = None,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    out = str(OUT)

    # Foreclosure source breakdown + upcoming auction callouts.
    # Runs off the local CSVs so it's the ONE authoritative view of
    # foreclosure records across all sources (APN + RL + T&B + HWM),
    # reconciling the split between main.py's --notify-slack post
    # (APN-only) and this consolidated daily-sweep post.
    forec_breakdown: list[str] = []
    upcoming_auctions: list[str] = []
    if csvs:
        forec_breakdown, upcoming_auctions = _analyze_foreclosure_csvs(csvs)

    lines = [
        f"*SiftStack Daily Sweep — {today}*",
        "_Jefferson · Madison · Marshall · Tier 1+2_",
        "",
    ]
    if upcoming_auctions:
        lines.extend(upcoming_auctions)
        lines.append("")
    if forec_breakdown:
        lines.extend(forec_breakdown)
        lines.append("")
    lines.extend([
        "*Foreclosure + Probate (main.py daily)*",
        *_format_main_funnel(main_f),
        "",
        "*APN Post-Probate*",
        *_format_apn_funnel(apn_f),
        "",
        "*Pre-Probate (Birmingham · Huntsville · Marshall)*",
        *_format_pre_funnel(pre_f),
        "",
    ])
    # Code Violation section — only render when the log had a funnel to
    # parse. Makes the 180-day cross-run dedup visible so "0-8 new/day"
    # doesn't read as a broken pipeline (it's the seen_code_violations
    # filter working as designed).
    if code_violation_f is not None and (
        code_violation_f.bulk_fetched or code_violation_f.dedup_fresh
        or code_violation_f.csv_written
    ):
        lines.extend([
            "*Code Violation (Huntsville · Birmingham · Hoover)*",
            *_format_code_violation_funnel(code_violation_f),
            "",
        ])

    if csv_count == 0:
        lines.append(
            ":information_source: No DataSift CSVs produced — see funnels above "
            "for which pipeline returned empty."
        )
    else:
        lines.append("*DataSift Uploads*")
        for r in results:
            ok = r.get("success", False)
            # Empty-CSV results (everything filtered by cross-run dedup) are
            # legitimate successes — show ⏭️ instead of ✅ so the operator
            # sees the difference at a glance, plus a tailored "no new
            # records" message instead of the upload count.
            if r.get("skipped_empty"):
                lines.append(
                    f"  ⏭️  {r['csv']}: no new records "
                    f"(all candidates already in DataSift via dedup)"
                )
                continue
            emoji = "✅" if ok else "❌"
            is_heir_csv = "_Heirs_" in r["csv"]
            # Two-pass architecture (see PASS 1 / PASS 2 in main()): the
            # same CSV can appear twice — once in mode=add (creates new
            # rows + updates by address) and once in mode=swap (replaces
            # owner/contact on existing rows). Show the mode tag so the
            # duplicate line doesn't read as an accidental double-upload.
            raw_mode = (r.get("mode") or "").lower()
            mode_tag = f" ({raw_mode})" if raw_mode in ("add", "swap") else ""
            sub = r.get("uploads", [])
            if sub:
                for u in sub:
                    base_label = u.get("list_name", "?")
                    nt = u.get("notice_type", "")
                    label = (
                        HEIRS_DISPLAY_LABELS.get(nt, base_label)
                        if is_heir_csv else base_label
                    )
                    lines.append(
                        f"  {emoji} {r['csv']}{mode_tag} → {label}: "
                        f"{u.get('records_uploaded','?')} records"
                    )
            else:
                lines.append(
                    f"  {emoji} {r['csv']}{mode_tag}: {r.get('message','')}"
                )
        lines.append("")

    # Always include a working link to the source-of-truth artifact for this
    # run. The "Output dir" line that used to appear here pointed at the
    # cloud runner's ephemeral filesystem (/home/runner/...) — useless to
    # the operator since that machine is gone the moment the job ends. The
    # artifact ZIP is what they actually need to download.
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "propsolvers-sketch/SiftStack")
    if run_id:
        run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
        lines.append(
            f"_:floppy_disk: <{run_url}|Download today's CSV bundle> "
            f"— scroll to the Artifacts section at the bottom of the run page._"
        )
        lines.append(f"_:scroll: <{run_url}|Live GitHub Actions log>_")
    else:
        # Local-run fallback — the operator IS at the machine that produced
        # these files, so showing the on-disk path is correct here.
        lines.append(f"_Output dir: {out}_")

    # Dropbox archive sync status (2026-06-12). One line so the operator
    # knows whether today's archive is already on their Mac via Dropbox.
    if dropbox_results:
        ok = sum(1 for r in dropbox_results if r["success"])
        total = len(dropbox_results)
        if ok == total:
            lines.append(
                f"_:open_file_folder: Dropbox: {ok}/{total} archives synced "
                f"to SiftStack/Archives/_"
            )
        elif ok > 0:
            lines.append(
                f"_:warning: Dropbox: {ok}/{total} archives synced "
                f"({total - ok} failed — see logs)_"
            )
        else:
            lines.append(
                f"_:warning: Dropbox sync failed for all {total} files "
                f"— check DROPBOX_* env vars_"
            )

    return "\n".join(lines)


async def main() -> int:
    if RUN_MARKER.exists():
        start_ts = RUN_MARKER.stat().st_mtime
    else:
        start_ts = time.time() - 6 * 3600
        print("WARN: no .finalize_start marker — fallback to last 6h", flush=True)

    # Parse pipeline logs (newest matching each pattern from this run)
    main_log = _newest_log("daily_main_*.log")
    apn_log = _newest_log("daily_apn_*.log")
    pre_log = _newest_log("daily_pre_*.log")
    code_log = _newest_log("daily_code_*.log")
    print(f"main log:        {main_log}", flush=True)
    print(f"apn log:         {apn_log}", flush=True)
    print(f"pre log:         {pre_log}", flush=True)
    print(f"code log:        {code_log}", flush=True)

    main_funnel = parse_main_daily(main_log) if main_log else MainDailyFunnel()
    apn_funnel = parse_apn_probate(apn_log) if apn_log else ApnProbateFunnel()
    pre_funnel = parse_pre_probate(pre_log) if pre_log else PreProbateFunnel()
    code_funnel = parse_code_violation(code_log) if code_log else CodeViolationFunnel()

    csvs = _csvs_from_this_run(start_ts)
    print(f"\nFound {len(csvs)} CSV(s) produced this run:", flush=True)
    for p in csvs:
        print(f"  {_categorize(p)}: {p.name}", flush=True)

    # ── Two-pass upload (operator-clarified workflow 2026-06-29) ──
    # Pass 1: Add-Data for ALL distressors — creates new records and
    #         updates existing ones by Property Address. Auto-maps the
    #         core fields (Property Address, Owner Name, Mailing,
    #         Tags). Runs DataSift's Enrich Data + Skip Trace once
    #         after each upload.
    # Pass 2: Swap-Owner for swap-eligible only (probate, pre_probate
    #         per should_swap_owners). Updates owner+phone+notes+tags
    #         on existing records via the dedicated "Swap owner of
    #         existing property" wizard mode. enrich + skip_trace
    #         skipped (Pass 1 already ran them).
    #
    # Sequencing rationale (operator's "completely last" requirement):
    # All Pass-1 Add-Data runs must complete BEFORE any Pass-2 swap
    # runs start, so a probate property's swap-mode owner update is
    # not re-overwritten by a later code-violation Add-Data upload on
    # the same address.
    #
    # History: pre-2026-06-29 was single-pass (mode="auto" → swap for
    # swap-eligible, add for others). That correctly handled owner
    # replacement on EXISTING records but silently dropped every new
    # property — operator-reported 2026-06-29 on 3 today-fresh pre-
    # probate addresses missing in DataSift (1287 BRIERFIELD, 1145
    # ALFORD, 820 AUGUST). The two-pass architecture closes that gap.
    from datasift_formatter import should_swap_owners as _should_swap

    results: list[dict] = []
    print("\n========== PASS 1: Add-Data (all distressors) ==========",
          flush=True)
    for p in csvs:
        label = _categorize(p)
        if label == "unknown":
            print(f"  SKIP {p.name} (could not categorize)", flush=True)
            continue
        results.append(await _upload_one(
            p, label, mode="add", enrich=True, skip_trace=True,
        ))

    # Pass 2: only swap-eligible CSVs, after ALL Pass-1 runs complete.
    swap_eligible = [
        p for p in csvs
        if _should_swap(_notice_type_from_csv(p))
        and _categorize(p) != "unknown"
    ]
    if swap_eligible:
        print(
            f"\n========== PASS 2: Swap-Owner (swap-eligible only — "
            f"{len(swap_eligible)} file(s)) ==========",
            flush=True,
        )
        for p in swap_eligible:
            label = _categorize(p)
            r = await _upload_one(
                p, label, mode="swap", enrich=False, skip_trace=False,
            )
            r["pass"] = 2
            results.append(r)

    # ── Dropbox archive sync ────────────────────────────────────────
    # Operator request 2026-06-12: upload the archive + per-distressor
    # files to Dropbox/SiftStack/Archives/ so they sync to the operator's
    # Mac automatically. Replaces the manual "download artifact ZIP"
    # workflow for daily auditing. Non-fatal if Dropbox is unreachable —
    # the run still succeeds based on DataSift upload outcomes.
    dropbox_results: list[dict] = []
    try:
        from dropbox_archive_uploader import upload_files as _dbx_upload
        # Upload BOTH the archive file (audit) AND every per-distressor
        # upload CSV (in case the operator wants to re-upload one by
        # hand later). All fresh files from this run, sourced from the
        # same output/leads/ directory.
        leads_dir = OUT / "leads"
        if leads_dir.exists():
            start_ts = RUN_MARKER.stat().st_mtime if RUN_MARKER.exists() else 0
            to_sync = sorted(
                p for p in leads_dir.glob("datasift_*.csv")
                if p.stat().st_mtime > start_ts
            )
            if to_sync:
                print(
                    f"\n=== Dropbox archive sync ({len(to_sync)} files) ===",
                    flush=True,
                )
                dropbox_results = _dbx_upload(to_sync)
                ok = sum(1 for r in dropbox_results if r["success"])
                print(
                    f"Dropbox: {ok}/{len(dropbox_results)} files synced",
                    flush=True,
                )
    except Exception as e:
        print(f"Dropbox archive sync skipped: {e}", flush=True)

    msg = _build_slack_message(
        main_funnel, apn_funnel, pre_funnel, results, len(csvs),
        dropbox_results=dropbox_results,
        csvs=csvs,
        code_violation_f=code_funnel,
    )
    print("\n--- SLACK ---", flush=True)
    print(msg, flush=True)
    posted = _send_webhook(msg)
    print(f"\nSlack posted: {posted}", flush=True)

    if results and not all(r.get("success") for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
