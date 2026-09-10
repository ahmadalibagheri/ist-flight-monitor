#!/usr/bin/env bash
#
# Export the collected observation history as a seed dataset.
#
#   ./scripts/export-seed.sh
#
# Writes backend/app/data/seed_dataset.sql.gz, which `app.cli seed` imports so a
# fresh checkout starts with a real history to compute analytics from rather than
# an empty dashboard.
#
# Only the tables that carry observation history are exported. Notably excluded:
#   - alembic_version   the schema version is owned by migrations, not by data
#   - daily_statistics / hourly_statistics
#                       these are a rebuildable cache over the observations; the
#                       importer recomputes them, so shipping them would only
#                       create a way for the two to disagree
#   - provider_health / sent_alerts
#                       per-deployment operational state, meaningless elsewhere
#
# Mock rows are excluded by the importer, not here, so a dev export stays usable.

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$PROJECT_DIR/backend/app/data/seed_dataset.sql.gz"
COMPOSE="${FLIGHTMON_COMPOSE:-docker compose}"

export PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"
cd "$PROJECT_DIR"
mkdir -p "$(dirname "$OUT")"

echo "Exporting observation history..."

# --column-inserts rather than the default COPY, for two reasons:
#   1. COPY carries its data on stdin using psql's own protocol, so the dump can
#      only be replayed by psql. Plain INSERTs can be executed by any driver,
#      which is what lets `app.cli seed` run inside the application's own
#      transaction and roll back cleanly on failure.
#   2. Naming every column means adding a nullable column later does not
#      invalidate a dataset already committed to the repository.
# It is more verbose, but this compresses to a handful of kilobytes.
$COMPOSE exec -T db pg_dump \
    --username=flightmon \
    --dbname=flightmon \
    --data-only \
    --no-owner \
    --no-privileges \
    --column-inserts \
    --table=airlines \
    --table=collection_runs \
    --table=flights \
    --table=flight_observations \
    --table=flight_events \
    | grep -vE '^\\(restrict|unrestrict)' \
    | gzip -9 > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "Wrote $OUT ($SIZE)"
echo
echo "Row counts in the export:"
$COMPOSE exec -T db psql -U flightmon -d flightmon -tAc "
    SELECT '  ' || relname || ': ' || n_live_tup
    FROM pg_stat_user_tables
    WHERE relname IN ('airlines','collection_runs','flights',
                      'flight_observations','flight_events')
    ORDER BY relname;"
