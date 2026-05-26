"""Send run summary notifications to Slack or Discord via webhook.

Works with both Slack incoming webhooks and Discord webhooks (using the
/slack compatibility endpoint). Set SLACK_WEBHOOK_URL in .env.

Discord webhook URLs should use the /slack suffix:
  https://discord.com/api/webhooks/{id}/{token}/slack
"""

import json
import logging
import os
from datetime import datetime

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Error & Warning Notifications ────────────────────────────────────


def _send_webhook(text: str, webhook_url: str | None = None) -> bool:
    """Send a plain-text message to the configured Slack/Discord webhook."""
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return False
    try:
        resp = requests.post(
            webhook_url,
            json={"text": text},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def notify_error(
    step: str,
    error: Exception | str,
    *,
    context: str = "",
    webhook_url: str | None = None,
) -> bool:
    """Send an error alert to Slack/Discord.

    Args:
        step: Pipeline step that failed (e.g., "Smarty Standardization").
        error: The exception or error message.
        context: Optional extra context (run_id, record count, etc.).
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":rotating_light: *SiftStack Pipeline Error*",
        f"*Step:* {step}",
        f"*Error:* {error}",
    ]
    if context:
        lines.append(f"*Context:* {context}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    text = "\n".join(lines)
    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Error notification sent to Slack: %s — %s", step, error)
    else:
        logger.warning("Could not send error notification (no webhook or send failed)")
    return sent


def notify_warning(
    message: str,
    *,
    context: str = "",
    webhook_url: str | None = None,
) -> bool:
    """Send a warning alert to Slack/Discord.

    Args:
        message: Warning description.
        context: Optional extra context.
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":warning: *SiftStack Warning*",
        f"{message}",
    ]
    if context:
        lines.append(f"*Context:* {context}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return _send_webhook("\n".join(lines), webhook_url)


def notify_preflight_failure(
    failures: list[str],
    *,
    webhook_url: str | None = None,
) -> bool:
    """Send a preflight check failure alert.

    Args:
        failures: List of failed check descriptions.
        webhook_url: Override webhook URL.

    Returns:
        True if notification sent successfully.
    """
    lines = [
        f":no_entry: *SiftStack Preflight Failed*",
        f"*{len(failures)} check(s) failed:*",
    ]
    for f in failures:
        lines.append(f"  - {f}")
    lines.append(f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("Pipeline did not start. Fix the above and re-run.")

    return _send_webhook("\n".join(lines), webhook_url)


def _count_by_field(notices: list[NoticeData], field: str) -> dict[str, int]:
    """Count notices grouped by a field value."""
    counts: dict[str, int] = {}
    for n in notices:
        val = getattr(n, field, "") or "unknown"
        counts[val] = counts.get(val, 0) + 1
    return counts


def _upcoming_auctions(notices: list[NoticeData], days: int = 7) -> list[dict]:
    """Find notices with auction dates in the next N days."""
    now = datetime.now()
    upcoming = []
    for n in notices:
        if not n.auction_date:
            continue
        try:
            auction_dt = datetime.strptime(n.auction_date, "%Y-%m-%d")
            delta = (auction_dt - now).days
            if 0 <= delta <= days:
                upcoming.append({
                    "address": n.address,
                    "city": n.city,
                    "date": n.auction_date,
                    "days_out": delta,
                    "type": n.notice_type,
                })
        except ValueError:
            continue
    return sorted(upcoming, key=lambda x: x["days_out"])


def build_summary(
    notices: list[NoticeData],
    *,
    upload_result: dict | None = None,
    elapsed_min: float = 0,
    api_cost: float = 0,
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,
    pdf_links: list[tuple[str, str]] | None = None,
) -> str:
    """Build a plain-text run summary for Slack/Discord.

    Args:
        notices: All notices from this run.
        upload_result: DataSift upload result dict (optional).
        elapsed_min: Pipeline elapsed time in minutes.
        api_cost: Estimated Haiku API cost for this run (legacy, use cost_breakdown).
        cost_breakdown: Dict of service -> cost, e.g. {"2Captcha": 0.09, "Tracerfy": 0.26}.
    """
    total = len(notices)
    by_county = _count_by_field(notices, "county")
    by_type = _count_by_field(notices, "notice_type")

    deceased = [n for n in notices if n.owner_deceased == "yes"]
    deceased_count = len(deceased)
    high_conf = sum(1 for n in deceased if n.dm_confidence == "high")
    med_conf = sum(1 for n in deceased if n.dm_confidence == "medium")
    low_conf = sum(1 for n in deceased if n.dm_confidence == "low")
    estate = sum(
        1 for n in deceased
        if n.decision_maker_relationship
        and "estate" in n.decision_maker_relationship.lower()
    )

    upcoming = _upcoming_auctions(notices)

    lines = [
        f"*SiftStack - Daily Report ({datetime.now().strftime('%Y-%m-%d')})*",
        "",
        f"*New notices scraped:* {total}",
    ]

    # County breakdown
    county_parts = [f"{v.title()}: {c}" for v, c in sorted(by_county.items())]
    if county_parts:
        lines.append(f"  {' | '.join(county_parts)}")

    # Type breakdown
    type_parts = [f"{t}: {c}" for t, c in sorted(by_type.items())]
    if type_parts:
        lines.append(f"  {' | '.join(type_parts)}")

    lines.append("")

    # Deceased owners
    if deceased_count > 0:
        pct = round(deceased_count / total * 100) if total else 0
        lines.append(f"*Deceased owners found:* {deceased_count} ({pct}%)")
        lines.append(f"  High confidence DM: {high_conf}")
        lines.append(f"  Medium confidence: {med_conf}")
        if low_conf:
            lines.append(f"  Low confidence: {low_conf}")
        if estate:
            lines.append(f"  Estate fallback: {estate}")
    else:
        lines.append("*Deceased owners found:* 0")

    # Upload result
    if upload_result:
        lines.append("")
        if upload_result.get("success"):
            lines.append(
                f"*Uploaded to DataSift:* {upload_result.get('records_uploaded', total)} records"
            )
        else:
            lines.append(
                f"*DataSift upload FAILED:* {upload_result.get('message', 'unknown error')}"
            )

    # Upcoming auctions
    if upcoming:
        lines.append("")
        lines.append(f"*Upcoming auctions (next 7 days):* {len(upcoming)}")
        for a in upcoming[:5]:
            lines.append(f"  {a['address']}, {a['city']} - {a['date']} ({a['days_out']}d)")
        if len(upcoming) > 5:
            lines.append(f"  ... and {len(upcoming) - 5} more")

    # Pipeline stats
    lines.append("")
    stats = []
    if elapsed_min > 0:
        stats.append(f"Pipeline: {elapsed_min:.0f} min")
    if api_cost > 0 and not cost_breakdown:
        stats.append(f"Haiku API: ${api_cost:.2f}")
    if stats:
        lines.append(" | ".join(stats))

    # File links (CSV + deep-prospecting PDFs)
    if csv_link or pdf_links:
        lines.append("")
        lines.append("*Files*")
        if csv_link:
            lines.append(f"  CSV: <{csv_link}|Download>")
        if pdf_links:
            lines.append(f"  PDFs ({len(pdf_links)}):")
            for addr, url in pdf_links[:10]:
                lines.append(f"    <{url}|{addr}>")
            if len(pdf_links) > 10:
                lines.append(f"    ... and {len(pdf_links) - 10} more")

    # Cost breakdown
    if cost_breakdown:
        total_cost = sum(cost_breakdown.values())
        lines.append("")
        lines.append(f"*Estimated run cost:* ${total_cost:.2f}")
        for service, cost in cost_breakdown.items():
            if cost > 0:
                lines.append(f"  {service}: ${cost:.2f}")

    return "\n".join(lines)


def send_slack_notification(
    notices: list[NoticeData],
    *,
    webhook_url: str | None = None,
    upload_result: dict | None = None,
    elapsed_min: float = 0,
    api_cost: float = 0,
    cost_breakdown: dict | None = None,
    csv_link: str | None = None,
    pdf_links: list[tuple[str, str]] | None = None,
) -> bool:
    """Send a run summary to Slack/Discord webhook.

    Args:
        notices: All notices from this run.
        webhook_url: Slack/Discord webhook URL (defaults to SLACK_WEBHOOK_URL env).
        upload_result: DataSift upload result dict.
        elapsed_min: Pipeline time in minutes.
        api_cost: Estimated API cost (legacy, use cost_breakdown).
        cost_breakdown: Dict of service -> cost for itemized cost reporting.

    Returns:
        True if notification sent successfully.
    """
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("No SLACK_WEBHOOK_URL set, skipping notification")
        return False

    text = build_summary(
        notices,
        upload_result=upload_result,
        elapsed_min=elapsed_min,
        api_cost=api_cost,
        cost_breakdown=cost_breakdown,
        csv_link=csv_link,
        pdf_links=pdf_links,
    )

    sent = _send_webhook(text, webhook_url)
    if sent:
        logger.info("Slack notification sent successfully")
    else:
        logger.error("Failed to send Slack notification")
    return sent


# ── Phase 2: Funnel + Service Rate Blocks (OPS-03, OBS-01) ────────────
#
# These two helpers build Slack Block Kit "section" dicts that each
# pipeline's notify_slack() appends to its existing blocks list before
# invoking the webhook sender. Pure functions — they construct dicts and
# return them; no HTTP, no I/O.
#
# Dependency direction is strictly one-way: this module does NOT import
# from src/observability.py. Callers build the per-run-rates and
# rolling-rates dicts using ServiceRateTracker + load_rolling_rates +
# rolling_rates_summary, then pass the resulting dicts in here. Keeping
# the dependency one-way means observability stays a pure data module
# and the Slack layer can be swapped (e.g. for Discord or a logger) with
# zero changes upstream.
#
# Block shape (Slack docs):
#     {"type": "section",
#      "text": {"type": "mrkdwn", "text": "..."}}
# The caller appends the returned dict to its `blocks` list. Wave 3
# (plan 02-03) adds the _send_blocks_webhook helper that posts a full
# blocks-array payload — _send_webhook above stays text-only.

# Display order for the service-rate block. Keys are the lowercase
# canonical names used by observability.TRACKED_SERVICES; the second
# tuple element is the Slack-rendered label (preserved casing). Order
# is fixed (2Captcha → Smarty → Tracerfy → LLM) per the plan spec — do
# NOT sort by per-run value or alphabetize, callers expect a stable
# visual scan-order across runs.
_RATE_DISPLAY_ORDER: tuple[tuple[str, str], ...] = (
    ("2captcha", "2Captcha"),
    ("smarty", "Smarty"),
    ("tracerfy", "Tracerfy"),
    ("llm", "LLM"),
)


def build_funnel_block(
    pipeline_name: str,
    gate_counts: dict,
) -> dict:
    """Build a Slack Block Kit section for a single pipeline's funnel.

    Renders the gate sequence as a bulleted list with per-gate counts.
    Iteration order matches the input dict's insertion order — both
    plain ``dict`` (Python 3.7+ guaranteed) and ``OrderedDict`` work.
    Pre-seeded zero-count gates render so the operator can tell "all
    dropped at obit-match" apart from "stage never ran".

    Args:
        pipeline_name: Human-readable identifier surfaced in the
            section header (e.g. ``"apn_probate"``, ``"benchmark"``).
        gate_counts: Mapping of gate name → count. Insertion order
            preserved in the rendered output (D-01 — per-pipeline gate
            sequence is sacred).

    Returns:
        A plain JSON-serialisable dict ready to be appended to a Slack
        ``blocks`` list. Shape:
            ``{"type": "section", "text": {"type": "mrkdwn", "text": "..."}}``
    """
    lines = [f"*Funnel — {pipeline_name}*"]
    for gate, count in gate_counts.items():
        lines.append(f"• {gate}: {count}")
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


def build_service_rates_block(
    per_run_rates: dict,
    rolling_rates: dict,
) -> dict:
    """Build a Slack Block Kit section for the 4-service rate panel.

    Renders one line per service in fixed display order (see
    ``_RATE_DISPLAY_ORDER``) with today's rate and the 7-day rolling
    rate side-by-side per D-03 ("Smarty: 86% today | 92% 7-day"). A
    ``None`` per-run rate renders as ``"n/a today"`` (no calls made);
    a ``None`` rolling rate renders as ``"— 7-day"`` (no historical
    baseline yet) — both visually distinct from a real 0% rate.

    Args:
        per_run_rates: Mapping of lowercase service name → float in
            [0.0, 1.0] OR None. Typically the output of
            ``ServiceRateTracker.per_run_rates()``.
        rolling_rates: Same shape as ``per_run_rates``. Typically the
            output of ``rolling_rates_summary(load_rolling_rates())``.

    Returns:
        A plain JSON-serialisable dict ready to be appended to a Slack
        ``blocks`` list.
    """
    lines = ["*Service Rates*"]
    for key, label in _RATE_DISPLAY_ORDER:
        per_run = per_run_rates.get(key)
        rolling = rolling_rates.get(key)

        per_run_str = (
            f"{round(per_run * 100)}% today" if per_run is not None else "n/a today"
        )
        rolling_str = (
            f"{round(rolling * 100)}% 7-day" if rolling is not None else "— 7-day"
        )

        lines.append(f"• {label}: {per_run_str} | {rolling_str}")

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


# ── Phase 2: Block-aware webhook send (OPS-03, OBS-01) ────────────────
#
# Pipelines call this DIRECTLY to post a Block Kit payload (text +
# blocks) — bypassing send_slack_notification entirely so the existing
# legacy `_send_webhook(text)` path stays byte-identical (plan 02-01's
# additive-only contract). Each pipeline's notify_slack() builds its
# own blocks list (existing summary section + build_funnel_block +
# build_service_rates_block) and POSTs the full payload in one HTTP
# call — no second Slack message, no thread (D-02: "one message, more
# content"). Mirrors _send_webhook's return contract: True on 200/204,
# False on anything else.


def _send_blocks_webhook(
    text: str,
    blocks: list[dict],
    webhook_url: str | None = None,
) -> bool:
    """Send {text, blocks} payload to the webhook (Phase 2: OPS-03, OBS-01).

    Args:
        text: Plain-text fallback. Slack renders this when the blocks
            payload fails to render (legacy clients, mobile, push
            notifications).
        blocks: Slack Block Kit list — each entry is a section / divider
            / etc. dict (typically built via ``build_funnel_block`` and
            ``build_service_rates_block``).
        webhook_url: Override the env-supplied SLACK_WEBHOOK_URL.

    Returns:
        True if the webhook returned HTTP 200 or 204. False on missing
        URL, network error, or non-2xx status. Same contract as
        ``_send_webhook`` so callers can treat them interchangeably for
        success-tracking purposes.
    """
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return False
    try:
        resp = requests.post(
            webhook_url,
            json={"text": text, "blocks": blocks},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False
