#!/usr/bin/env bash
#
# Hourly collection driver for cron.
#
#   crontab -e
#   7 */8 * * * /path/to/ist-flight-monitor/scripts/collect.sh
#
# Runs one collection cycle inside a throwaway container using the same image,
# environment and database as the rest of the stack, then aggregates statistics
# and dispatches any Telegram alerts.
#
# IMPORTANT: use EITHER this cron job OR the `worker` service, never both.
# Running both doubles your provider API usage and halves your quota.
# To switch to cron:      docker compose stop worker   (or SCHEDULER_ENABLED=false)
#
# Exit codes: 0 ok | 1 collection failed | 2 misconfigured | 3 another run in progress

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${FLIGHTMON_LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="$LOG_DIR/collect.log"
LOCK_FILE="${FLIGHTMON_LOCK_FILE:-/tmp/flightmon-collect.lock}"
COMPOSE="${FLIGHTMON_COMPOSE:-docker compose}"

mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE"; }

# cron gives a minimal PATH; docker usually lives outside it.
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"

# flock stops a slow or hung cycle from being run over by the next hour's.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "SKIP  another collection is still running (lock: $LOCK_FILE)"
    exit 3
fi

cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
    log "ERROR no .env found in $PROJECT_DIR - copy .env.example and configure a provider"
    exit 2
fi

log "START collection cycle"

set +e
OUTPUT="$($COMPOSE run --rm --no-deps \
            -e SCHEDULER_ENABLED=false \
            api python -m app.cli collect 2>&1)"
STATUS=$?
set -e

printf '%s\n' "$OUTPUT" >>"$LOG_FILE"

case $STATUS in
    0)
        SEEN=$(printf '%s' "$OUTPUT" | grep -o '"flights_seen": *[0-9]*' | grep -o '[0-9]*' | head -1)
        OBS=$(printf '%s' "$OUTPUT" | grep -o '"observations_written": *[0-9]*' | grep -o '[0-9]*' | head -1)
        log "OK    flights_seen=${SEEN:-?} observations_written=${OBS:-?}"
        ;;
    2)  log "ERROR misconfigured - no configured provider in PROVIDER_CHAIN" ;;
    *)  log "ERROR collection failed (exit $STATUS)" ;;
esac

# Keep the log from growing without bound; cron jobs outlive attention spans.
if [[ -f "$LOG_FILE" ]] && [[ $(wc -c <"$LOG_FILE") -gt 10485760 ]]; then
    tail -c 2097152 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    log "INFO  log truncated"
fi

exit $STATUS
