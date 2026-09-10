# IST Flight Reliability Monitor

Monitors every **direct departure from Istanbul Airport (IST)** to
**Tehran Imam Khomeini (IKA)** and **Mashhad (MHD)**, stores an append-only history of
what each flight was doing at every point in time, and turns that history into delay,
cancellation, time-of-day and reliability analytics — delivered as an hourly Telegram
report, a REST API, and a web dashboard.

```
provider chain ──▶ collector ──▶ PostgreSQL ──▶ analytics ──▶ ┬─▶ REST API ──▶ dashboard
 (hourly)          (append-only)  (history)     (aggregates)  └─▶ Telegram reports/alerts
```

---

## Quick start

```bash
git clone <this-repo> && cd airport
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD and one provider API key (see below).
docker compose up -d
```

Then open:

| | |
|---|---|
| Dashboard | <http://localhost:3000> |
| API docs (Swagger) | <http://localhost:8000/docs> |
| Health probe | <http://localhost:8000/health> |
| Readiness probe | <http://localhost:8000/ready> |

Migrations run automatically on API start. The worker collects immediately, then on
your configured interval.

> **`/ready` returns `ready: false` until a real provider is configured.** That is
> deliberate — without one the service starts fine but can never collect anything, and a
> green probe would hide that.

---

## 1. Configure a flight data provider (required)

The system will not invent data. You need one real API key.
Full comparison, limits and setup: **[docs/PROVIDERS.md](docs/PROVIDERS.md)**.

### Recommended: AeroDataBox (has a free tier)

1. Go to <https://rapidapi.com/aedbx-aedbx/api/aerodatabox> and subscribe to the
   **Basic** plan (free, 600 requests/month — enough for hourly collection).
2. Copy your RapidAPI key into `.env`:

   ```dotenv
   PROVIDER_CHAIN=aerodatabox
   AERODATABOX_API_KEY=your_rapidapi_key
   AERODATABOX_BASE_URL=https://aerodatabox.p.rapidapi.com
   AERODATABOX_AUTH_HEADER=X-RapidAPI-Key
   AERODATABOX_HOST_HEADER=aerodatabox.p.rapidapi.com
   ```

3. Restart and confirm:

   ```bash
   docker compose up -d
   curl -s localhost:8000/providers | python3 -m json.tool
   curl -X POST localhost:8000/api/v1/admin/collect   # collect now, don't wait an hour
   ```

### Alternatives

| Provider | Key setting | Notes |
|---|---|---|
| AviationStack | `AVIATIONSTACK_API_KEY` | Cheap in call count. Free tier is **HTTP-only** and ~100 req/month. |
| FlightAware AeroAPI | `FLIGHTAWARE_API_KEY` | Highest quality, billed per result. |

Chain them for failover — the first configured, healthy provider that returns data wins:

```dotenv
PROVIDER_CHAIN=aerodatabox,flightaware
PROVIDER_STRATEGY=failover
```

### Enrichment: combining providers

`PROVIDER_STRATEGY=merge` queries **every** configured provider each cycle and combines
their views of the same flight, so a source that knows the gate fills in for one that only
knows the schedule.

Four rules govern the merge, in order:

1. **Trust order is chain order.** The leftmost provider wins a contested field.
2. **Observed beats predicted, regardless of trust.** A provider reporting an *actual*
   departure outranks a higher-trust provider that only has an estimate — ground truth is
   not a matter of vendor reputation. This is what lets a free ADS-B feed correct a paid
   schedule API.
3. **Status and delay are re-derived, never voted on.** After timings are merged, both are
   recomputed from the merged values. Taking a status from one provider and times from
   another is how you get a `SCHEDULED` flight that has already departed.
4. **Cancellation needs the most-trusted opinion, and disagreement is recorded.** A false
   cancellation is more damaging than a late one, so a lower-trust provider cannot override
   a higher-trust one — the disagreement is logged as a conflict instead of being discarded.

Each provider's own view is still stored as its own `flight_observations` row, so the
per-source history survives the merge and a rule can be revisited later against what each
provider actually said. `flights.field_sources` records which provider supplied each field:

```sql
SELECT flight_number, data_source, field_sources FROM flights;
-- TK878 | aerodatabox+opensky | {"gate": "aerodatabox", "actual_departure_utc": "opensky", ...}
```

Merge mode costs one call set per provider per cycle — budget quota accordingly. With a
single provider configured it behaves exactly like failover.

### Running without a key (development)

```dotenv
ENVIRONMENT=development
ALLOW_MOCK_PROVIDER=true
PROVIDER_CHAIN=mock
```

This generates **synthetic** flights so the UI and scheduler can be exercised. They are
tagged `is_mock=true` and **excluded from every statistic**, so the dashboard will
correctly report "No real flight data collected yet". The mock provider refuses to run
when `ENVIRONMENT=production`.

---

## 2. Configure Telegram (optional)

1. Message **[@BotFather](https://t.me/BotFather)** → `/newbot` → copy the token.
2. Send your new bot any message (or add it to a group and post there).
3. Fetch your chat id:

   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
     | python3 -c "import json,sys;print([u['message']['chat']['id'] for u in json.load(sys.stdin)['result']])"
   ```

   Group ids are negative (e.g. `-1001234567890`).

4. Put both in `.env` and restart:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_CHAT_ID=-1001234567890
   TELEGRAM_ENABLED=true
   ```

5. Verify delivery:

   ```bash
   curl -X POST localhost:8000/api/v1/admin/telegram/test
   ```

You will then receive the hourly report plus alerts for new cancellations, delays
crossing 60 and 120 minutes, and route-wide disruptions. **Alerts are never duplicated** —
each is fingerprinted and the fingerprint is `UNIQUE` in the database, so a restart or a
second worker cannot re-send one.

Leave `TELEGRAM_BOT_TOKEN` blank to disable Telegram entirely.

---

## How to read the numbers

The most important design decisions are about *denominators*. Getting these wrong is how
monitoring systems quietly flatter airlines.

| Metric | Denominator |
|---|---|
| `cancellation_rate` | **All** scheduled flights — a cancelled flight is still a flight that was scheduled. |
| `delay_rate`, `on_time_rate` | **Measurable** flights only: not cancelled, and with a known delay. |
| `severe_delay_rate` | Measurable flights delayed more than 60 minutes. |

Three rules are enforced throughout and covered by tests:

1. **Missing data is never a cancellation.** A flight that disappears from a provider's
   board is marked `data_quality = STALE`; its status is left untouched.
2. **An unknown delay is never zero.** If the provider gave no usable timing, the flight
   is counted in `unknown_flights` and excluded from delay and on-time rates — counting
   it as on-time would be a lie. The dashboard renders it as `—`, never `0`.
3. **A re-timed flight cannot pass as punctual.** Airlines move schedules, and
   providers overwrite `scheduledTime` when they do — so delay measured against the
   *current* schedule reads ~0 however far the flight shifted. A live example on
   IST→IKA moved **315 minutes** and scored as perfectly on time. `flights` therefore
   keeps `first_scheduled_departure_utc` (set once, never updated) and exposes
   `schedule_moved_minutes` and `total_displacement_minutes`; the board marks such
   flights `retimed +5h`. `delay_minutes` keeps its industry meaning — displacement is
   reported alongside it, not folded into it.
4. **Thin samples are never ranked.** An airline needs `MIN_SAMPLE_SIZE_AIRLINE`
   (default 20) flights, a flight number 10, a time period 10. Below that, entries are
   returned with `is_ranked: false` and sorted last. Comparative claims ("best time to
   fly") additionally require at least two qualifying periods — with only one, the same
   window would be named both best and worst.
5. **But a repeated cancellation is always reported.** The sample-size rule cuts both
   ways, and was hiding the most actionable fact in the data: a flight cancelled every
   day it was scheduled sits below the ranking minimum and appears nowhere. Three
   cancellations out of three is not a small sample to discount — it is a pattern a
   traveller must be told. `GET /statistics/repeat-cancellations` and the hourly
   report's `REPEATEDLY CANCELLED` section are exempt from the threshold; the ranking
   itself still respects it.

### Reliability score

A transparent 0–100 weighted sum. Every weight and anchor is an environment variable, and
`GET /api/v1/statistics` returns the full component breakdown so any score can be explained:

```
score = 100 × ( 0.35 × on_time_rate
              + 0.30 × (1 − min(cancellation_rate / 0.25, 1))
              + 0.20 × (1 − min(avg_delay_minutes / 90, 1))
              + 0.15 × (1 − min(severe_delay_rate / 0.35, 1)) )
```

Weights are validated to sum to 1.0 at startup.

---

## Timezone handling

- Everything is **stored in UTC** (`TIMESTAMPTZ`), and the database container runs in UTC.
- Everything is **reported in `Europe/Istanbul`** (configurable via `OPERATIONAL_TIMEZONE`).
- Each flight carries denormalised local projections (`flight_date_local`,
  `scheduled_hour_local`, `scheduled_dow_local`) so hour-of-day analytics group by *local*
  hour without a per-query conversion. A 23:30 UTC departure correctly counts as 02:30 the
  next local day.
- `local_day_bounds()` derives a day's end from the *next local midnight*, so it stays
  correct across DST transitions.

---

## Architecture

```
backend/
├── app/
│   ├── core/            config, structured logging, enums, timezone helpers, DB session
│   ├── models/          SQLAlchemy models (10 tables)
│   ├── schemas/         Pydantic request/response models
│   ├── providers/       provider abstraction + AeroDataBox / AviationStack / FlightAware / mock
│   ├── services/        the collection cycle
│   ├── analytics/       metrics, reliability scoring, dimensions, aggregation, insights
│   ├── reports/         hourly + historical report building and rendering
│   ├── notifications/   Telegram delivery and alert rules
│   ├── scheduler/       APScheduler jobs and the worker entry point
│   ├── api/routes/      FastAPI routers
│   └── main.py
├── migrations/          Alembic
└── tests/               249 tests
frontend/                Next.js 14 + Recharts dashboard
docs/                    PROVIDERS.md, DEPLOYMENT.md
```

### Data model

| Table | Purpose |
|---|---|
| `flights` | One row per flight leg per local day. Latest known state. |
| `flight_observations` | **Append-only.** One snapshot per flight per collection cycle. Never updated. Includes `raw_payload` (JSONB): the provider's response stored verbatim. |
| `flight_events` | Derived deltas: status changed, delay increased, cancelled, gate changed… |
| `daily_statistics` / `hourly_statistics` | Materialised aggregates by route / airline / flight / period / day-of-week. A rebuildable cache over the observation history. |
| `collection_runs` | What ran, which provider answered, what it wrote. |
| `provider_health` | Failure counts and cooldowns driving failover. |
| `sent_alerts` | Alert ledger; `UNIQUE(dedup_key)` is what prevents duplicate Telegram messages. |
| `airports` / `airlines` | Reference data. Airlines are discovered from the feed. |

The append-only history is the point: `TK878` observed at 10:00 as `SCHEDULED +0`, at
11:00 as `DELAYED +35`, and at 13:00 as `DEPARTED +70` produces three permanent rows, not
one mutated one.

**Why `raw_payload` exists.** A provider will not tell you what it said about a flight last
Tuesday — historical responses cannot be re-fetched. Any field not persisted at collection
time is lost permanently. Alongside the modelled columns, each observation therefore keeps
the provider's response verbatim (~1 KB), so a field nobody thought to model today is still
recoverable from history tomorrow. Set `STORE_RAW_PAYLOAD=false` to opt out.

It also preserves signals the schema does not model — AeroDataBox's own `quality` array,
for instance, distinguishes a `["Basic"]` reading (schedule only) from `["Basic","Live"]`
(actively tracked). That difference decides whether "no delay" means *on time* or merely
*not being watched*:

```sql
SELECT f.flight_number,
       o.raw_payload #>> '{departure,quality}' AS quality
FROM flight_observations o JOIN flights f ON f.id = o.flight_id;
```

---

## Configuration

Every setting is an environment variable; see **`.env.example`** for the annotated list.
The most load-bearing ones:

| Variable | Default | Meaning |
|---|---|---|
| `PROVIDER_CHAIN` | `aerodatabox` | Ordered failover list. |
| `COLLECTION_INTERVAL_MINUTES` | `60` | One of 5, 15, 30, 60. |
| `COLLECTION_LOOKAHEAD_HOURS` | `36` | How far ahead each cycle polls. |
| `STALE_AFTER_MINUTES` | `180` | Absent this long → marked stale (**not** cancelled). |
| `DELAY_THRESHOLD_MINUTES` | `15` | When a flight counts as delayed. |
| `MIN_SAMPLE_SIZE_AIRLINE` | `20` | Below this an airline is never ranked. |
| `PROVIDER_CACHE_TTL_SECONDS` | `240` | Cost control: dedupes identical upstream calls. |
| `OPERATIONAL_TIMEZONE` | `Europe/Istanbul` | Reporting timezone. |

`GET /api/v1/reports/config` returns the effective non-secret configuration of a running
instance.

---

## API

Interactive docs at `/docs`. Highlights:

| Endpoint | Purpose |
|---|---|
| `GET /health`, `GET /ready` | Liveness and deep readiness. |
| `GET /providers` | Provider inventory and health. Never echoes keys. |
| `GET /api/v1/flights` | Filter by route, airline, flight number, status, date range. |
| `GET /api/v1/flights/{flight_number}` | Every observed instance of a flight number. |
| `GET /api/v1/flights/{id}` | One flight with its **full observation and event history**. |
| `GET /api/v1/routes`, `/airlines` | Reference data. |
| `GET /api/v1/statistics` | Headline metrics + reliability breakdown. |
| `GET /api/v1/statistics/delays`, `/cancellations` | Focused views. |
| `GET /api/v1/statistics/hourly` | Metrics per local hour. |
| `GET /api/v1/statistics/time-of-day` | Best / worst period per route. |
| `GET /api/v1/statistics/airlines`, `/flights` | Reliability rankings. |
| `GET /api/v1/statistics/repeat-cancellations` | Flights cancelled on multiple days, exempt from the sample-size minimum. |
| `GET /api/v1/statistics/by/{scope}` | Group by `MONTH`, `DOW`, `PERIOD`, `AIRLINE`, `FLIGHT`, `ROUTE`… |
| `GET /api/v1/statistics/daily`, `/trends` | Time series and period-over-period change. |
| `GET /api/v1/reports/latest` | Hourly report (JSON, plus rendered text). |
| `GET /api/v1/reports/latest.txt` | The exact fixed-width report sent to Telegram. |
| `GET /api/v1/reports/daily`, `/monthly` | Rolling historical summaries. |
| `POST /api/v1/admin/collect` | Run a collection cycle now. |
| `POST /api/v1/admin/aggregate` | Rebuild materialised statistics. |
| `POST /api/v1/admin/telegram/test` | Verify Telegram delivery. |

All windows accept `?window=today|7d|30d|90d|mtd|all` or explicit
`?start_date=&end_date=`, plus `route`, `airline` and `flight_number` filters.

> The `/admin` endpoints mutate state and ship **unauthenticated**. Keep them behind your
> reverse proxy, VPN, or an ingress rule — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Dashboard

Nine sections: Overview, Tehran, Mashhad, Airlines, Flights, Delays, Cancellations,
Time-of-day analytics, Historical trends. Charts cover cancellations and delays over time,
cancellation and delay rate by hour, airline reliability, route comparison, and daily
volume. Filters (date range, route, airline, flight number) apply across sections.

---

## Development

```bash
cd backend
python3 -m venv .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python -   # if pip is missing
.venv/bin/pip install -r requirements-dev.txt

# A database for development and tests
docker run -d --name flightmon-dev-db \
  -e POSTGRES_USER=flightmon -e POSTGRES_PASSWORD=devpassword -e POSTGRES_DB=flightmon \
  -e PGTZ=UTC -p 55432:5432 postgres:16-alpine
docker exec flightmon-dev-db psql -U flightmon -d flightmon -c "CREATE DATABASE flightmon_test;"

export DATABASE_URL="postgresql+psycopg://flightmon:devpassword@localhost:55432/flightmon"
export TEST_DATABASE_URL="postgresql+psycopg://flightmon:devpassword@localhost:55432/flightmon_test"

.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Quality gates:

```bash
.venv/bin/python -m pytest      # 249 tests
.venv/bin/ruff check .
.venv/bin/mypy app
```

Integration tests skip cleanly when `TEST_DATABASE_URL` is unset, so `pytest` always runs
on a bare checkout. The test schema is built by the **real Alembic migrations**, so a
migration that drifts from the models fails the suite rather than the deployment.

Frontend:

```bash
cd frontend && npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
npm run typecheck && npm run build
```

---

## Scheduling: worker or cron (pick one)

Collection runs on a schedule either way. **Do not enable both** — every cycle would
run twice and burn double your provider quota.

### Option A — the `worker` container (default)

APScheduler inside `docker compose`. Nothing to install; `COLLECTION_INTERVAL_MINUTES`
controls the cadence.

### Option B — system cron

Useful when you want host-level control, or no long-running worker process.

```bash
# 1. turn off the in-process scheduler
docker compose stop worker
sed -i 's/^SCHEDULER_ENABLED=.*/SCHEDULER_ENABLED=false/' .env

# 2. point the schedule at this checkout, then install it
#    (cron needs absolute paths and does not expand ~)
sed -i "s|/path/to/ist-flight-monitor|$(pwd)|g" scripts/crontab.example
crontab scripts/crontab.example
crontab -l
```

| Script | Default schedule | What it does |
|---|---|---|
| `scripts/collect.sh` | `7 * * * *` | One collection cycle, then aggregate + Telegram alerts |
| `scripts/daily-report.sh` | `0 23 * * *` | Historical summary to Telegram |
| `scripts/healthcheck.sh` | `35 * * * *` | Exits non-zero if collection has stalled, so cron mails you |

`collect.sh` takes an exclusive `flock`, so a slow cycle is skipped rather than run over
by the next hour's (exit code `3`). Output goes to `logs/collect.log`, truncated at 10 MB.
`CRON_TZ=Europe/Istanbul` in the crontab means the daily report fires at 23:00 *local*.

Each script shells out to the CLI, which is also usable directly:

```bash
docker compose run --rm --no-deps api python -m app.cli collect
docker compose run --rm --no-deps api python -m app.cli aggregate --days 30
docker compose run --rm --no-deps api python -m app.cli report --kind daily --send
docker compose run --rm --no-deps api python -m app.cli status     # non-zero if stalled
```

Exit codes are the contract cron relies on: `0` ok, `1` collection failed,
`2` misconfigured (no provider key), `3` another run in progress.

### Quota

AeroDataBox caps one request at 12 hours, so:

```
API calls per cycle = ceil((COLLECTION_LOOKBACK_HOURS + COLLECTION_LOOKAHEAD_HOURS) / 12)
```

The shipped default (`3` + `9` = 12h) is **one call per cycle** — 24/day, which keeps the
600-unit free tier alive for ~25 days. Widening the window to 42 hours quadruples that.
To collect less often under cron, change the schedule to `7 */2 * * *`.

---

## Operations

```bash
docker compose logs -f worker           # collection cycles
docker compose logs -f api
curl -s localhost:8000/api/v1/reports/collection-runs | python3 -m json.tool
curl -s localhost:8000/providers | python3 -m json.tool
```

Logs are structured JSON (`LOG_FORMAT=console` for human-readable local output) covering
collection start/end, provider responses and latency, flights collected and updated,
cancellations and delays detected, report generation, Telegram delivery, and errors.

**Rebuilding statistics** is always safe — they are a cache over the observation history:

```bash
curl -X POST "localhost:8000/api/v1/admin/aggregate?start_date=2026-08-01&end_date=2026-09-06"
```

Production deployment, backups, scaling and security hardening:
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

---

## Cost control

- One provider call serves both routes (AeroDataBox returns the whole airport board).
- Responses are cached per request window for `PROVIDER_CACHE_TTL_SECONDS`.
- Only the configured routes are ever requested; everything else is discarded at the
  provider boundary.
- Already-collected history is never re-fetched — the collector polls a bounded window
  around now (`COLLECTION_LOOKBACK_HOURS` / `COLLECTION_LOOKAHEAD_HOURS`).
- Failing providers enter a cooldown instead of being retried every cycle.

At the default hourly interval with AeroDataBox, one day costs roughly 72–96 API units.

---

## Security

- No credential has a default value; the app starts without them and reports what is
  missing at `/ready`.
- `.env` is git-ignored; only `.env.example` is tracked.
- `GET /api/v1/reports/config` and `GET /providers` expose configuration *shape* and a
  boolean `configured` flag — never key material. A test asserts this.
- Containers run as non-root.
- Compose refuses to start without `POSTGRES_PASSWORD` set.

---

## Licence

Provided as-is. Flight data remains subject to each provider's terms of service.
