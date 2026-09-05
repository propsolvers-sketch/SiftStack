"""SiftStack weekly health-check.

Catches silent-failure classes we've hit in the past — DataSift endpoint
shape changes, vendor tag write regressions, cascade re-processing,
expired vendor credentials, cost spikes.

Runs Sunday from daily-sweep.yml. Posts one Slack message with a per-
group ✅/⚠️/❌ status board. Non-fatal — individual checks catch their
own exceptions so one broken vendor doesn't blank out the whole report.

CLI:
    python scripts/health_check.py              # run all checks, post Slack
    python scripts/health_check.py --no-slack   # print only, no post
    python scripts/health_check.py --group cascade  # run one group

Groups:
    api        DataSift /property/ list + detail endpoint contracts
    tags       Vendor tag matrix — sample recent records per lane
    cascade    Cascade behavior trends (dedup firing, no re-processing)
    vendors    External vendor API ping (Tracerfy/Enformion/Trestle/Smarty/Dropbox/Slack)
    infra      cron-job.org PAT expiry, GHA run success
    cost       Trestle/Enformion/SmartSkip 7-day trend + anomaly detection
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import datasift_api as ds

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Result plumbing
# ─────────────────────────────────────────────────────────────────────


STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"

_STATUS_EMOJI = {
    STATUS_OK: "✅",
    STATUS_WARN: "⚠️",
    STATUS_FAIL: "❌",
}


@dataclass
class CheckResult:
    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class GroupResult:
    name: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def worst_status(self) -> str:
        # FAIL > WARN > OK
        for s in (STATUS_FAIL, STATUS_WARN, STATUS_OK):
            if any(c.status == s for c in self.checks):
                return s
        return STATUS_OK


def _ok(name: str, msg: str, details: list[str] | None = None) -> CheckResult:
    return CheckResult(name=name, status=STATUS_OK, summary=msg, details=details or [])


def _warn(name: str, msg: str, details: list[str] | None = None) -> CheckResult:
    return CheckResult(name=name, status=STATUS_WARN, summary=msg, details=details or [])


def _fail(name: str, msg: str, details: list[str] | None = None) -> CheckResult:
    return CheckResult(name=name, status=STATUS_FAIL, summary=msg, details=details or [])


def _safe(fn):
    """Wrap a check so any exception becomes a FAIL result instead of aborting the run."""
    def wrapper(*args, **kwargs) -> CheckResult:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return _fail(fn.__name__, f"exception: {type(e).__name__}: {e}")
    return wrapper


# ─────────────────────────────────────────────────────────────────────
# Group 1 — DataSift API contract
# ─────────────────────────────────────────────────────────────────────


@_safe
def check_datasift_list_endpoint() -> CheckResult:
    """Catches the class of bug where an endpoint response shape changes.

    Historically bit us: /property/ list started returning tags=None +
    lists=None (only tag_count / list_count populated). We ONLY notice
    this when we try to filter by tag/list and every check silently
    no-ops. This asserts the current expected shape."""
    resp = ds._get("/property/", {"limit": 3, "ordering": "-created"})
    page = resp.get("data") or resp.get("results") or []
    if not page:
        return _fail("datasift_list_endpoint", "empty page from /property/")
    row = page[0]
    required_keys = {"uuid", "address", "tag_count", "list_count"}
    missing = required_keys - set(row.keys())
    if missing:
        return _fail(
            "datasift_list_endpoint",
            f"missing keys in list response: {sorted(missing)}",
        )
    # Under the CURRENT known contract, list endpoint returns tags=None
    # and detail endpoint returns tags=[...]. If tags= starts returning
    # a populated array in the list response, that's a shape change
    # worth noting (we could skip detail fetch and save time).
    if row.get("tags") not in (None, []):
        return _warn(
            "datasift_list_endpoint",
            "list endpoint now returns tags in-band — health_check + "
            "thorough_skip_trace can drop the per-record detail fetch",
        )
    return _ok("datasift_list_endpoint",
               f"OK (tag_count={row.get('tag_count')}, list_count={row.get('list_count')})")


@_safe
def check_datasift_detail_endpoint() -> CheckResult:
    """Verify /property/{uuid} returns tags + lists arrays (not None)."""
    resp = ds._get("/property/", {"limit": 1, "ordering": "-created"})
    page = resp.get("data") or resp.get("results") or []
    if not page:
        return _fail("datasift_detail_endpoint", "no records to test detail fetch on")
    uuid = page[0]["uuid"]
    detail = ds.get_property(uuid)
    tags = detail.get("tags")
    lists = detail.get("lists")
    if tags is None:
        return _fail("datasift_detail_endpoint",
                     f"detail endpoint returned tags=None for {uuid[:8]} — "
                     "cascade dedup depends on this; investigate immediately")
    if not isinstance(tags, list):
        return _fail("datasift_detail_endpoint",
                     f"detail tags is {type(tags).__name__}, expected list")
    return _ok("datasift_detail_endpoint",
               f"OK ({uuid[:8]} → {len(tags)} tags, {len(lists or [])} lists)")


@_safe
def check_address_index_builds() -> CheckResult:
    """The street|zip5 address index is the ONLY sound row->UUID mapping
    (DataSift ignores ?search= and ?lists=). Path A capture, courthouse
    snapshots, subtype/curated tag promotion, and the backfills all depend
    on it. On 2026-09-05 we found it had been silently EMPTY for days:
    DataSift 400s at offset>=10K and that exception escaped before the
    index was assigned, so every lookup returned None. Build it and assert
    it's populated."""
    idx = ds.build_property_index(ordering="-created")
    n = len(idx)
    if n == 0:
        return _fail("address_index_builds",
                     "address index built with 0 entries — every UUID lookup "
                     "will return None (Path A / snapshots / tag promotion dead)")
    if n < 1000:
        return _warn("address_index_builds",
                     f"address index only {n} entries — expected several thousand")
    return _ok("address_index_builds", f"index built: {n} street|zip5 entries")


@_safe
def check_datasift_lists_exist() -> CheckResult:
    """Every pipeline expects certain DataSift lists to exist. If any of
    the canonical list titles have been renamed / deleted, the
    NOTICE_TYPE_TO_LIST mapping in datasift_formatter breaks + records
    land in the wrong list."""
    expected = [
        "Foreclosure",
        "Probate",
        "Pre-Probate",
        "Pre-Probate/Deceased",
        "Tax Delinquent",
        "Code Violation",
    ]
    resp = ds._get("/list/", {"limit": 200})
    lists = resp.get("data") or resp.get("results") or []
    have = {l.get("title", "") for l in lists}
    missing = [name for name in expected if name not in have]
    if missing:
        return _fail("datasift_lists_exist",
                     f"missing expected DataSift lists: {missing}")
    return _ok("datasift_lists_exist",
               f"OK ({len(expected)} canonical lists present)")


# ─────────────────────────────────────────────────────────────────────
# Group 2 — Vendor tag matrix integrity
# ─────────────────────────────────────────────────────────────────────


def _sample_records_in_lists(list_titles: set[str], n: int = 5,
                              max_pages: int = 5) -> list[dict]:
    """Fetch N most recent records that appear in ANY of the given lists.

    Two important design choices (both learned the hard way 2026-09-01):

    1. Uses ``ordering=-updated`` (not -created) — DataSift address-dedups
       new uploads against existing properties, so today's list-adds keep
       the record's OLD created timestamp. -created buries them past the
       fetch cap; -updated surfaces them (their updated_at bumps when the
       list membership changes).

    2. Accepts a SET of list titles (union) — a "standard lane" or "probate
       lane" spans multiple DataSift lists, so sampling a single list
       misses records that landed in a sibling list. Union covers all of
       them in one pass.

    Walks up to ``max_pages`` × 100 records to find N samples. Client-side
    filter because DataSift's /property/ endpoint doesn't accept a list
    membership URL param (verified 2026-08-31 diagnostic)."""
    matches = []
    offset = 0
    limit = 100
    for _ in range(max_pages):
        resp = ds._get("/property/",
                       {"limit": limit, "offset": offset, "ordering": "-updated"})
        page = resp.get("data") or resp.get("results") or []
        if not page:
            break
        for row in page:
            if not row.get("list_count"):
                continue
            try:
                detail = ds.get_property(row["uuid"])
            except Exception:
                continue
            rec_lists = {
                (entry if isinstance(entry, str) else entry.get("title", ""))
                for entry in (detail.get("lists") or [])
            }
            if rec_lists & list_titles:
                matches.append(detail)
                if len(matches) >= n:
                    return matches
        if len(page) < limit:
            break
        offset += limit
    return matches


def _tag_titles_lc(rec: dict) -> set[str]:
    titles = set()
    for t in (rec.get("tags") or []):
        title = t if isinstance(t, str) else (t.get("title") or t.get("name") or "")
        if title:
            titles.add(title.strip().lower())
    return titles


# Full DataSift list-title sets per cascade lane. Union sampling covers
# every list in the lane in one pass so a check doesn't miss a lane's
# records just because they landed in a sibling list. Sourced from
# datasift_formatter.NOTICE_TYPE_TO_LIST + the probate-universe list
# canonicalized in the two-lane cascade policy (2026-08-31).
_STANDARD_LANE_LISTS = {
    "Foreclosure", "Code Violation", "Tax Delinquent", "Eviction", "Divorce",
}
_PROBATE_LANE_LISTS = {
    "Probate", "Pre-Probate", "Obituary", "Inherited",
    "Estate and Heirs", "Estate Sales", "Pre-Probate/Deceased",
    "Probate Properties",
}


@_safe
def check_standard_lane_tags() -> CheckResult:
    """Sample 5 recent standard-lane records (from ANY of Foreclosure /
    Code Violation / Tax Delinquent / Eviction / Divorce). Under new
    policy (2026-08-31) they should have traced_tracerfy +
    traced_datasift. traced_enformion is optional (skipped in standard
    cascade)."""
    samples = _sample_records_in_lists(_STANDARD_LANE_LISTS, n=5)
    if not samples:
        return _warn("standard_lane_tags",
                     "no standard-lane records found in recent 500 — "
                     f"expected any of {sorted(_STANDARD_LANE_LISTS)}")
    required = {"traced_tracerfy", "traced_datasift"}
    missing_by_uuid = []
    for rec in samples:
        tags = _tag_titles_lc(rec)
        gap = required - tags
        if gap:
            missing_by_uuid.append(f"{rec['uuid'][:8]} missing {sorted(gap)}")
    if missing_by_uuid:
        return _warn(
            "standard_lane_tags",
            f"{len(missing_by_uuid)}/{len(samples)} standard-lane records "
            f"missing expected vendor tags — cascade may not be reaching them",
            details=missing_by_uuid,
        )
    return _ok("standard_lane_tags",
               f"{len(samples)}/{len(samples)} standard-lane records fully tagged "
               f"(tracerfy + datasift)")


@_safe
def check_probate_lane_tags() -> CheckResult:
    """Sample 5 recent probate-universe records (from ANY of Probate /
    Pre-Probate / Obituary / Inherited / Estate and Heirs / Estate Sales
    / Pre-Probate/Deceased / Probate Properties). Under policy they
    should have ALL 4: traced_tracerfy + traced_datasift +
    traced_enformion + (traced_smartskip OR smartskip_no_match)."""
    samples = _sample_records_in_lists(_PROBATE_LANE_LISTS, n=5)
    if not samples:
        return _warn("probate_lane_tags",
                     "no probate-lane records found in recent 500 — "
                     f"expected any of {sorted(_PROBATE_LANE_LISTS)}")
    required_core = {"traced_tracerfy", "traced_datasift", "traced_enformion"}
    smartskip_signals = {"traced_smartskip", "smartskip_no_match"}
    issues = []
    for rec in samples:
        tags = _tag_titles_lc(rec)
        gap = required_core - tags
        if gap:
            issues.append(f"{rec['uuid'][:8]} missing {sorted(gap)}")
        if not (tags & smartskip_signals):
            issues.append(f"{rec['uuid'][:8]} missing SmartSkip signal "
                          f"(neither traced_smartskip nor smartskip_no_match)")
    if issues:
        return _warn(
            "probate_lane_tags",
            f"{len(issues)} issues across {len(samples)} probate-lane records",
            details=issues,
        )
    return _ok("probate_lane_tags",
               f"{len(samples)}/{len(samples)} probate-lane records fully tagged "
               f"(4-vendor stack + SmartSkip signal)")


# ─────────────────────────────────────────────────────────────────────
# Group 3 — Cascade behavior trends (last 7 days)
# ─────────────────────────────────────────────────────────────────────


LOGS_DIR = Path(__file__).parent.parent / "logs"


def _parse_cascade_log_metrics(log_path: Path) -> dict[str, int]:
    """Extract dropped_* + Records-to-process counts from one cascade log."""
    if not log_path.exists():
        return {}
    text = log_path.read_text(errors="ignore")
    m = re.search(
        r"Fetched (\d+) records after all filters.*"
        r"dropped_off_tier=(\d+), dropped_probate_universe=(\d+), "
        r"dropped_already_complete=(\d+)",
        text,
    )
    if not m:
        return {}
    return {
        "kept": int(m.group(1)),
        "off_tier": int(m.group(2)),
        "probate_universe": int(m.group(3)),
        "already_complete": int(m.group(4)),
    }


@_safe
def check_cascade_dedup_firing() -> CheckResult:
    """Read last 7 days of cascade logs. If dropped_already_complete is 0
    across all runs, dedup isn't kicking in — same class of bug as the
    2026-09-01 tags=None issue."""
    if not LOGS_DIR.exists():
        return _warn("cascade_dedup_firing", "logs/ directory not found")
    logs = sorted(LOGS_DIR.glob("thorough_skip_trace_*.log"))[-7:]
    if not logs:
        return _warn("cascade_dedup_firing", "no cascade logs found in last 7 days")
    zero_dedup_days = []
    metrics_by_day = []
    for log in logs:
        m = _parse_cascade_log_metrics(log)
        if not m:
            continue
        metrics_by_day.append(f"{log.stem}: kept={m['kept']} skipped={m['already_complete']}")
        if m["already_complete"] == 0 and m["kept"] > 5:
            # Zero dedup with meaningful processing = suspicious
            zero_dedup_days.append(log.stem)
    if zero_dedup_days:
        return _fail(
            "cascade_dedup_firing",
            f"dropped_already_complete=0 on {len(zero_dedup_days)} recent runs — "
            f"dedup likely broken",
            details=zero_dedup_days,
        )
    return _ok(
        "cascade_dedup_firing",
        f"dedup firing across {len(metrics_by_day)} recent runs",
        details=metrics_by_day[-3:],
    )


@_safe
def check_code_violation_sources() -> CheckResult:
    """Catch code-violation adapters that silently produce zero.

    Discovered 2026-09-05: Hoover SeeClickFix returned 0 rows every day
    from 2026-06-13 (feed went dry / geo query drifted to Birmingham) and
    the Huntsville Unsafe-Buildings PDF parser extracted 0 records after
    a format change — both for MONTHS, with only Birmingham Accela still
    flowing. Nothing alerted. This reads the most recent daily_code_*.log
    and flags each source's dry signature."""
    if not LOGS_DIR.exists():
        return _warn("code_violation_sources", "logs/ directory not found")
    logs = sorted(LOGS_DIR.glob("daily_code_*.log"))
    if not logs:
        return _warn("code_violation_sources", "no daily_code_*.log found")
    text = logs[-1].read_text(errors="ignore")
    issues: list[str] = []
    # Hoover source is the council resolutions page since 2026-09-05 (the
    # SeeClickFix feed went private). Zero nuisance rows can be legitimate
    # for a week or two between council meetings — only flag a parse/page
    # failure, or a full month of nothing.
    if "Hoover council resolutions page appears EMPTY or CHANGED" in text:
        issues.append("Hoover: resolutions page parsed <3 rows (layout change / WAF?)")
    m = re.search(r"Hoover council resolutions: (\d+) nuisance resolutions kept "
                  r"\(page rows=(\d+), nuisance=(\d+)", text)
    if m and int(m.group(3)) == 0 and int(m.group(2)) >= 3:
        issues.append("Hoover: page parsed but 0 nuisance rows classified (title wording change?)")
    if "Hoover SeeClickFix" in text:
        issues.append("Hoover: retired SeeClickFix adapter still being invoked")
    if re.search(r"Parsed 0 unsafe-building records", text):
        issues.append("Huntsville: PDF parser extracted 0 records (format change?)")
    m = re.search(r"Funnel \(code_violation\): .*'bulk_fetched': (\d+)", text)
    if m and int(m.group(1)) == 0:
        issues.append("All sources: bulk_fetched=0")
    if issues:
        return _warn("code_violation_sources",
                     f"{len(issues)} source(s) producing nothing ({logs[-1].name})",
                     details=issues)
    return _ok("code_violation_sources", f"all sources flowing ({logs[-1].name})")


# ─────────────────────────────────────────────────────────────────────
# Group 4 — External vendor APIs
# ─────────────────────────────────────────────────────────────────────


@_safe
def check_tracerfy_creds() -> CheckResult:
    key = os.environ.get("TRACERFY_API_KEY", "")
    if not key:
        return _fail("tracerfy_creds", "TRACERFY_API_KEY not set")
    if len(key) < 20:
        return _warn("tracerfy_creds", f"key looks suspiciously short ({len(key)} chars)")
    return _ok("tracerfy_creds", "TRACERFY_API_KEY set")


@_safe
def check_enformion_ping() -> CheckResult:
    try:
        import enformion_client as enf
    except Exception as e:
        return _fail("enformion_ping", f"import failed: {e}")
    if not enf.is_configured():
        return _fail("enformion_ping",
                     "ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD not set")
    return _ok("enformion_ping", "credentials present")


@_safe
def check_trestle_ping() -> CheckResult:
    key = os.environ.get("TRESTLE_API_KEY", "")
    if not key:
        return _fail("trestle_ping", "TRESTLE_API_KEY not set")
    try:
        import phone_validator as pv
        # Known-safe number that always returns a response
        res = pv.call_trestle("2055551212", key)
    except Exception as e:
        return _fail("trestle_ping", f"call failed: {type(e).__name__}: {e}")
    if not isinstance(res, dict):
        return _fail("trestle_ping", f"unexpected response type: {type(res).__name__}")
    return _ok("trestle_ping", "responded normally")


@_safe
def check_smarty_creds() -> CheckResult:
    if not (os.environ.get("SMARTY_AUTH_ID") and os.environ.get("SMARTY_AUTH_TOKEN")):
        return _fail("smarty_creds", "SMARTY_AUTH_ID / SMARTY_AUTH_TOKEN not set")
    return _ok("smarty_creds", "credentials present")


@_safe
def check_dropbox_ping() -> CheckResult:
    try:
        from dropbox_archive_uploader import _get_client
        dbx = _get_client()
        # Cheap call: get current account
        acct = dbx.users_get_current_account()
    except Exception as e:
        return _fail("dropbox_ping", f"call failed: {type(e).__name__}: {e}")
    return _ok("dropbox_ping",
               f"authenticated as {getattr(acct, 'email', '?')}")


@_safe
def check_slack_webhook() -> CheckResult:
    url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url:
        return _fail("slack_webhook", "SLACK_WEBHOOK_URL not set")
    if not url.startswith(("https://hooks.slack.com/", "https://discord.com/api/webhooks/")):
        return _warn("slack_webhook", f"URL doesn't look like a known webhook host")
    return _ok("slack_webhook", "URL present")


# ─────────────────────────────────────────────────────────────────────
# Group 5 — Infrastructure / cron
# ─────────────────────────────────────────────────────────────────────


@_safe
def check_daily_run_recency() -> CheckResult:
    """Verify a daily-sweep bot commit landed in the last 48h. If cron
    stopped firing (PAT expired, cron-job.org broken), we go silent
    for days before noticing."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci", "--author=github-actions"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception as e:
        return _warn("daily_run_recency", f"git log failed: {e}")
    if not out:
        return _fail("daily_run_recency", "no bot commits found in git log")
    # Parse "2026-09-01 09:19:52 +0000" style
    try:
        commit_dt = datetime.strptime(out.split()[0] + " " + out.split()[1],
                                       "%Y-%m-%d %H:%M:%S")
    except Exception:
        return _warn("daily_run_recency", f"unparseable commit date: {out}")
    hours_ago = (datetime.now() - commit_dt).total_seconds() / 3600
    if hours_ago > 48:
        return _fail("daily_run_recency",
                     f"last bot commit was {hours_ago:.0f}h ago — cron may be broken")
    if hours_ago > 30:
        return _warn("daily_run_recency",
                     f"last bot commit was {hours_ago:.0f}h ago — expected <26h")
    return _ok("daily_run_recency",
               f"last bot commit {hours_ago:.1f}h ago")


# ─────────────────────────────────────────────────────────────────────
# Group 6 — Cost anomaly detection
# ─────────────────────────────────────────────────────────────────────


@_safe
def check_trestle_cost_trend() -> CheckResult:
    """Parse last 7 daily cascade logs, compute Trestle phone-scored total.
    Alert if today's is >3x the 7-day median (excluding today)."""
    if not LOGS_DIR.exists():
        return _warn("trestle_cost_trend", "logs/ dir not found")
    logs = sorted(LOGS_DIR.glob("thorough_skip_trace_*.log"))[-8:]
    daily_phones = []
    for log in logs:
        text = log.read_text(errors="ignore")
        m = re.search(r"Phones tiered via Trestle:\s+(\d+)", text)
        if m:
            daily_phones.append((log.stem, int(m.group(1))))
    if len(daily_phones) < 3:
        return _warn("trestle_cost_trend",
                     f"only {len(daily_phones)} days of data — need >=3 for trend")
    counts = [c for _, c in daily_phones]
    today = counts[-1]
    prior = sorted(counts[:-1])
    median = prior[len(prior) // 2] if prior else 0
    today_cost = today * 0.05
    if median > 0 and today > median * 3 and today > 200:
        return _warn(
            "trestle_cost_trend",
            f"today={today} phones scored (${today_cost:.2f}) vs 7-day "
            f"median={median} — >3x spike",
        )
    return _ok(
        "trestle_cost_trend",
        f"today={today} phones (${today_cost:.2f}) vs median={median}",
    )


# ─────────────────────────────────────────────────────────────────────
# Orchestration + Slack
# ─────────────────────────────────────────────────────────────────────


GROUPS = {
    "api": [check_datasift_list_endpoint, check_datasift_detail_endpoint,
            check_datasift_lists_exist, check_address_index_builds],
    "tags": [check_standard_lane_tags, check_probate_lane_tags],
    "cascade": [check_cascade_dedup_firing],
    "sources": [check_code_violation_sources],
    "vendors": [check_tracerfy_creds, check_enformion_ping, check_trestle_ping,
                check_smarty_creds, check_dropbox_ping, check_slack_webhook],
    "infra": [check_daily_run_recency],
    "cost": [check_trestle_cost_trend],
}


GROUP_LABELS = {
    "api": "DataSift API Contracts",
    "tags": "Vendor Tag Integrity",
    "cascade": "Cascade Behavior",
    "sources": "Code-Violation Sources",
    "vendors": "External Vendor APIs",
    "infra": "Infrastructure / Cron",
    "cost": "Cost Trend",
}


def run_group(group_name: str) -> GroupResult:
    group = GroupResult(name=group_name)
    for fn in GROUPS.get(group_name, []):
        result = fn()
        group.checks.append(result)
        logger.info("  %s %s — %s", _STATUS_EMOJI[result.status],
                    result.name, result.summary)
    return group


def format_slack_message(groups: list[GroupResult]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    total_fail = sum(1 for g in groups for c in g.checks if c.status == STATUS_FAIL)
    total_warn = sum(1 for g in groups for c in g.checks if c.status == STATUS_WARN)
    total_ok = sum(1 for g in groups for c in g.checks if c.status == STATUS_OK)
    total = total_fail + total_warn + total_ok
    overall_emoji = "🚨" if total_fail else ("⚠️" if total_warn else "🩺")

    # Header intentionally omits "SiftStack" — the webhook payload sets
    # username="SiftStack Health Bot" + icon_emoji=":stethoscope:" so the
    # sender identity already carries the branding. Repeating it in the
    # header wastes vertical space.
    lines = [
        f"*{overall_emoji} Weekly Health Check — {today}*",
        f"_Passed: {total_ok}/{total}  ·  Warnings: {total_warn}  ·  Failures: {total_fail}_",
        "",
    ]
    for g in groups:
        emoji = _STATUS_EMOJI[g.worst_status]
        lines.append(f"*{emoji} {GROUP_LABELS.get(g.name, g.name)}*")
        for c in g.checks:
            check_emoji = _STATUS_EMOJI[c.status]
            lines.append(f"  {check_emoji} {c.name} — {c.summary}")
            for d in c.details[:5]:
                lines.append(f"      · {d}")
        lines.append("")
    lines.append("_Runs Sundays via GHA. To run ad-hoc: `python scripts/health_check.py`_")
    return "\n".join(lines)


def post_slack(msg: str) -> bool:
    """Post to Slack via a DEDICATED webhook so this appears as its own
    integration in the workspace's Activity view.

    Env var priority:
      1. HEALTH_SLACK_WEBHOOK_URL  ← preferred; separate Slack app named
                                     "SiftStack Health Bot" so Activity
                                     view groups it separately from
                                     "DataSift" (the daily-sweep webhook)
      2. SLACK_WEBHOOK_URL         ← fallback for local dev / when the
                                     dedicated webhook isn't configured

    Payload also sets username + icon_emoji as a safety net — Slack's
    per-message override wins over the webhook's configured name, so
    even if a future setup change repoints HEALTH_SLACK_WEBHOOK_URL at
    a differently-named app, the branding stays consistent.

    Discord (via /slack shim) honors username; ignores icon_emoji.
    """
    url = os.environ.get("HEALTH_SLACK_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url:
        logger.warning(
            "Neither HEALTH_SLACK_WEBHOOK_URL nor SLACK_WEBHOOK_URL set — printing only"
        )
        return False
    using_dedicated = bool(os.environ.get("HEALTH_SLACK_WEBHOOK_URL"))
    logger.info("Posting to %s webhook",
                "dedicated health-check" if using_dedicated else "fallback (daily-sweep)")
    # Uses `requests` (not stdlib urllib) — stdlib depends on the OS SSL
    # cert bundle which is unreliable on macOS (raises CERTIFICATE_VERIFY_FAILED
    # when the system Python doesn't have certifi's CA bundle wired up).
    # `requests` bundles certifi internally, so it works consistently on Mac
    # AND on the GHA Ubuntu runner. Same library src/slack_notifier.py uses
    # for the daily-sweep webhook post.
    try:
        resp = requests.post(
            url,
            json={
                "text": msg,
                "username": "SiftStack Health Bot",
                "icon_emoji": ":stethoscope:",
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return 200 <= resp.status_code < 300
    except Exception as e:
        logger.warning("Slack post failed: %s", e)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group", choices=list(GROUPS.keys()),
                    help="Run only one group (default: all)")
    ap.add_argument("--no-slack", action="store_true",
                    help="Print results, don't post to Slack")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    which = [args.group] if args.group else list(GROUPS.keys())
    results = []
    for g in which:
        logger.info("── %s ──", GROUP_LABELS.get(g, g))
        results.append(run_group(g))

    msg = format_slack_message(results)
    print("")
    print(msg)

    if not args.no_slack:
        posted = post_slack(msg)
        print(f"\nSlack posted: {posted}")

    # Exit code: 1 if any FAIL, else 0. Warns don't fail the run.
    any_fail = any(c.status == STATUS_FAIL for g in results for c in g.checks)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
