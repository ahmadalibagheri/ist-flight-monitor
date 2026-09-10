#!/usr/bin/env bash
#
# Alerts if collection has stalled. Exits non-zero when the newest run is older
# than two collection intervals, or the last run failed - so cron mails you.
#
#   30 * * * * /path/to/ist-flight-monitor/scripts/healthcheck.sh

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="${FLIGHTMON_COMPOSE:-docker compose}"

export PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"
cd "$PROJECT_DIR"

$COMPOSE run --rm --no-deps -e SCHEDULER_ENABLED=false api python -m app.cli status
