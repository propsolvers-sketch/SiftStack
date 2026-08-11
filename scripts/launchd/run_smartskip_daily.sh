#!/usr/bin/env bash
# Daily SmartSkip cron wrapper — invoked by launchd at 1:00 AM CST.
#
# Responsibilities:
#   1. cd to SiftStack repo root + load .env
#   2. Run probate_cascade.py with production flags:
#        --rehash-only           only records not yet SmartSkip'd (dedup)
#        --max-cost-usd 10       $10 hard cap = ~66 records max per run
#        --and-standard-cascade  chains Tracerfy+DataSift+Enformion+Trestle
#        --notify-slack          combined Slack post
#   3. Log everything to logs/smartskip_launchd_YYYYMMDD.log for troubleshooting
#   4. On any Python error, post a distinct Slack alert so operator knows
#      to investigate (session expiry, quota, etc.)

set -uo pipefail   # error handling below (avoid -e so we can Slack-alert)

REPO_ROOT="/Users/shanismith/Desktop/SiftStack"
cd "$REPO_ROOT" || exit 1

# Load .env so DATASIFT_API_KEY, TRESTLE_API_KEY, SLACK_WEBHOOK_URL etc are set
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
fi

# Daily log with date stamp
LOG_DATE="$(date +%Y%m%d)"
LOG_FILE="$REPO_ROOT/logs/smartskip_launchd_${LOG_DATE}.log"
mkdir -p "$REPO_ROOT/logs"

{
    echo "═════════════════════════════════════════════════════════════"
    echo "SmartSkip launchd cron — $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "═════════════════════════════════════════════════════════════"
} >> "$LOG_FILE"

# Run the cascade. Uses the operator's venv Python.
"$REPO_ROOT/.venv/bin/python" \
    "$REPO_ROOT/scripts/probate_cascade.py" \
    --rehash-only \
    --max-cost-usd 10 \
    --and-standard-cascade \
    --notify-slack \
    2>&1 | tee -a "$LOG_FILE"

# Capture exit code from the Python process (not tee)
PY_EXIT="${PIPESTATUS[0]}"

if [ "$PY_EXIT" -ne 0 ]; then
    echo "" >> "$LOG_FILE"
    echo "⚠ probate_cascade exited with code $PY_EXIT" >> "$LOG_FILE"

    # Post a distinct Slack alert on cron failure so operator investigates.
    # Silent-fail if no webhook is set.
    if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
        ALERT_MSG="⚠ *SmartSkip daily cron FAILED* — exit code $PY_EXIT\n"
        ALERT_MSG+="Log: $LOG_FILE\n"
        ALERT_MSG+="Common causes:\n"
        ALERT_MSG+="  • SmartSkip session expired → run scratch/smartskip_recon/launch_and_explore.py to re-login\n"
        ALERT_MSG+="  • DataSift API rate limit → auto-recovers next run\n"
        ALERT_MSG+="  • CC declined on payment step → check app.smartskip.io/billing"
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"text\": \"$ALERT_MSG\"}" \
            "$SLACK_WEBHOOK_URL" > /dev/null 2>&1
    fi
    exit "$PY_EXIT"
fi

exit 0
