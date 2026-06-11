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
from datetime import datetime
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

    header: list[str] | None = None
    rows_written = 0
    with open(out_path, "w", newline="", encoding="utf-8") as out_f:
        writer = None
        for src in group:
            try:
                with open(src, newline="", encoding="utf-8") as in_f:
                    reader = csv.DictReader(in_f)
                    if header is None:
                        header = list(reader.fieldnames or [])
                        writer = csv.DictWriter(out_f, fieldnames=header)
                        writer.writeheader()
                    for row in reader:
                        writer.writerow(row)
                        rows_written += 1
            except Exception as e:
                logger = __import__("logging").getLogger(__name__)
                logger.warning("Skipping %s during consolidation: %s", src, e)
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

async def _upload_one(p: Path, label: str) -> dict:
    print(f"\n=== Uploading {p.name} ({label}) ===", flush=True)
    try:
        r = await upload_to_datasift_per_distressor(
            p, headless=True, enrich=True, skip_trace=True,
        )
    except Exception as e:
        r = {"success": False, "message": f"exception: {e}", "uploads": []}
    r["csv"] = p.name
    r["label"] = label
    print(f"{p.name}: success={r.get('success')} {r.get('message','')}", flush=True)
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


def _build_slack_message(
    main_f: MainDailyFunnel,
    apn_f: ApnProbateFunnel,
    pre_f: PreProbateFunnel,
    results: list[dict],
    csv_count: int,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    out = str(OUT)
    lines = [
        f"*SiftStack Daily Sweep — {today}*",
        "_Jefferson · Madison · Marshall · Tier 1+2_",
        "",
        "*Foreclosure + Probate (main.py daily)*",
        *_format_main_funnel(main_f),
        "",
        "*APN Post-Probate*",
        *_format_apn_funnel(apn_f),
        "",
        "*Pre-Probate (Birmingham · Huntsville · Marshall)*",
        *_format_pre_funnel(pre_f),
        "",
    ]

    if csv_count == 0:
        lines.append(
            ":information_source: No DataSift CSVs produced — see funnels above "
            "for which pipeline returned empty."
        )
    else:
        lines.append("*DataSift Uploads*")
        for r in results:
            ok = r.get("success", False)
            emoji = "✅" if ok else "❌"
            is_heir_csv = "_Heirs_" in r["csv"]
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
                        f"  {emoji} {r['csv']} → {label}: "
                        f"{u.get('records_uploaded','?')} records"
                    )
            else:
                lines.append(f"  {emoji} {r['csv']}: {r.get('message','')}")
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
    print(f"main log:        {main_log}", flush=True)
    print(f"apn log:         {apn_log}", flush=True)
    print(f"pre log:         {pre_log}", flush=True)

    main_funnel = parse_main_daily(main_log) if main_log else MainDailyFunnel()
    apn_funnel = parse_apn_probate(apn_log) if apn_log else ApnProbateFunnel()
    pre_funnel = parse_pre_probate(pre_log) if pre_log else PreProbateFunnel()

    csvs = _csvs_from_this_run(start_ts)
    print(f"\nFound {len(csvs)} CSV(s) produced this run:", flush=True)
    for p in csvs:
        print(f"  {_categorize(p)}: {p.name}", flush=True)

    results: list[dict] = []
    for p in csvs:
        label = _categorize(p)
        if label == "unknown":
            print(f"  SKIP {p.name} (could not categorize)", flush=True)
            continue
        results.append(await _upload_one(p, label))

    msg = _build_slack_message(
        main_funnel, apn_funnel, pre_funnel, results, len(csvs),
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
