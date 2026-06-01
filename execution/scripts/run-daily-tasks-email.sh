#!/usr/bin/env bash
# Run daily tasks email and send to DAILY_TASKS_TO_EMAIL.
# Source .env from ateles repo root so cron does not need env vars in crontab.
#
# Cron (daily at 7:00 AM):
#   0 7 * * * /path/to/ateles/execution/scripts/run-daily-tasks-email.sh
#
# Requires in .env: DATA_DIR, EMAIL_DELIVERY_API_KEY, DAILY_TASKS_TO_EMAIL
# Optional: EMAIL_DELIVERY_API (resend|sendgrid|mailgun), NEWSLETTER_FROM_EMAIL

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ATELES_ROOT"
# shellcheck source=/dev/null
[ -f .env ] && source .env
exec python3 "$SCRIPT_DIR/daily_tasks_email.py" --send
