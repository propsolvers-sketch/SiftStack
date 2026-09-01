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
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

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


def _sample_records_in_list(list_title: str, n: int = 5) -> list[dict]:
    """Fetch N most recent records that are in the given list. Uses list
    endpoint + client-side filter (list endpoint doesn't support server-
    side list filter — no cross-list URL param)."""
    resp = ds._get("/property/", {"limit": 100, "ordering": "-created"})
    page = resp.get("data") or resp.get("results") or []
    matches = []
    # list_count>0 pre-filter, then detail fetch to confirm membership
    for row in page:
        if not row.get("list_count"):
            continue
        try:
            detail = ds.get_property(row["uuid"])
        except Exception:
            continue
        list_titles = [
            (entry if isinstance(entry, str) else entry.get("title", ""))
            for entry in (detail.get("lists") or [])
        ]
        if list_title in list_titles:
            matches.append(detail)
            if len(matches) >= n:
                break
    return matches


def _tag_titles_lc(rec: dict) -> set[str]:
    titles = set()
    for t in (rec.get("tags") or []):
        title = t if isinstance(t, str) else (t.get("title") or t.get("name") or "")
        if title:
            titles.add(title.strip().lower())
    return titles


@_safe
def check_standard_lane_tags() -> CheckResult:
    """Sample 5 recent Foreclosure records. Under new policy (2026-08-31)
    they should have traced_tracerfy + traced_datasift. traced_enformion
    is optional (skipped in standard cascade)."""
    samples = _sample_records_in_list("Foreclosure", n=5)
    if not samples:
        return _warn("standard_lane_tags",
                     "no Foreclosure records found in recent 100 — nothing to check")
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
            f"{len(missing_by_uuid)}/{len(samples)} Foreclosure records missing "
            f"expected vendor tags — cascade may not be reaching them",
            details=missing_by_uuid,
        )
    return _ok("standard_lane_tags",
               f"{len(samples)}/{len(samples)} Foreclosure records fully tagged "
               f"(tracerfy + datasift)")


@_safe
def check_probate_lane_tags() -> CheckResult:
    """Sample 5 recent Probate records. Under policy they should have
    ALL 4: traced_tracerfy + traced_datasift + traced_enformion +
    (traced_smartskip OR smartskip_no_match)."""
    samples = _sample_records_in_list("Probate", n=5)
    if not samples:
        return _warn("probate_lane_tags",
                     "no Probate records found in recent 100 — nothing to check")
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
            f"{len(issues)} issues across {len(samples)} Probate records",
            details=issues,
        )
    return _ok("probate_lane_tags",
               f"{len(samples)}/{len(samples)} Probate records fully tagged "
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
    "api": [check_datasift_list_endpoint, check_datasift_detail_endpoint, check_datasift_lists_exist],
    "tags": [check_standard_lane_tags, check_probate_lane_tags],
    "cascade": [check_cascade_dedup_firing],
    "vendors": [check_tracerfy_creds, check_enformion_ping, check_trestle_ping,
                check_smarty_creds, check_dropbox_ping, check_slack_webhook],
    "infra": [check_daily_run_recency],
    "cost": [check_trestle_cost_trend],
}


GROUP_LABELS = {
    "api": "DataSift API Contracts",
    "tags": "Vendor Tag Integrity",
    "cascade": "Cascade Behavior",
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
    body = json.dumps({
        "text": msg,
        "username": "SiftStack Health Bot",
        "icon_emoji": ":stethoscope:",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
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
