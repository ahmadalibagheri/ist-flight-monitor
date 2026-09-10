#!/usr/bin/env bash
#
# Daily historical summary, delivered to Telegram.
#
#   0 23 * * * /path/to/ist-flight-monitor/scripts/daily-report.sh
#
# Schedule this in the operational timezone (Europe/Istanbul) - see CRON_TZ in
# the crontab, or set the host timezone accordingly.

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${FLIGHTMON_LOG_DIR:-$PROJECT_DIR/logs}"
COMPOSE="${FLIGHTMON_COMPOSE:-docker compose}"

mkdir -p "$LOG_DIR"
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"
cd "$PROJECT_DIR"

{
    printf '%s START daily report\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    $COMPOSE run --rm --no-deps -e SCHEDULER_ENABLED=false \
        api python -m app.cli report --kind daily --send --days 30
    printf '%s DONE\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >>"$LOG_DIR/daily-report.log" 2>&1
