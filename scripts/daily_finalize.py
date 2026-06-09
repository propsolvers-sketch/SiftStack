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
    """Find all datasift CSVs written this run. Pipelines now drop them in
    output/leads/ (post-reorg). We still glob output/ root for backward-compat
    so a partial transition or legacy script invocation still gets picked up."""
    candidates: list[Path] = []
    for root in (OUT / "leads", OUT):
        if root.exists():
            candidates.extend(root.glob("datasift_upload*.csv"))
    return sorted(
        {p for p in candidates if p.stat().st_mtime > after_ts}
    )


def _categorize(p: Path) -> str:
    if "_DMs_" in p.name:
        return "main_dms"
    if "_Heirs_" in p.name:
        return "main_heirs"
    try:
        with open(p, newline="", encoding="utf-8") as f:
            row = next(csv.DictReader(f), None)
        if row:
            nt = (row.get("Notice Type") or "").strip()
            if nt == "pre_probate":
                return "pre_probate"
            if nt == "probate":
                return "apn_probate"
    except Exception:
        pass
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
        lines.append(f"_Output dir: {out}_")

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "propsolvers-sketch/SiftStack")
    if run_id:
        lines.append(
            f"_<https://github.com/{repo}/actions/runs/{run_id}|GitHub Actions log>_"
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
