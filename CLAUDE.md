# CLAUDE.md

Guidance for AI assistants (and new contributors) working in this repository.

## What this is

A flight reliability monitor for direct departures **IST → IKA** and **IST → MHD**. It
polls real flight-status APIs, stores an append-only observation history, and derives
delay / cancellation / time-of-day / reliability analytics, an hourly Telegram report, a
REST API and a dashboard.

## Non-negotiable rules

These are the invariants the whole system is built around. Breaking one silently corrupts
months of statistics, so they are covered by tests — check them before changing anything
in `providers/`, `services/` or `analytics/`.

1. **Never fabricate flight data.** No synthetic rows in production paths, no
   placeholder statuses, no invented timestamps. If a provider did not supply a value,
   the column stays `NULL`.
2. **Observations are append-only.** `flight_observations` rows are inserted, never
   updated or deleted. `flights` carries the latest state as a convenience only, and must
   remain reconstructible from the observation history.
3. **Missing data is never a cancellation.** A flight absent from a provider board gets
   `data_quality = STALE`; its `status` is untouched.
   (`collector.py::_mark_stale`, `normalization.py::normalize_status`)
4. **An unknown delay is never zero.** `compute_delay_minutes()` returns `None` when no
   timing pair supports a measurement. `None` is excluded from delay rates and averages,
   and rendered `—` in the UI.
5. **Mock data never reaches analytics.** Everything from `MockProvider` is tagged
   `is_mock = true`. `analytics/queries.py::_base_select` filters it out, and that filter
   must stay in the shared base select rather than being repeated per query.
6. **Thin samples are never ranked, but repeated cancellations are always reported.**
   Respect `MIN_SAMPLE_SIZE_*` for rankings: entries below the minimum get
   `is_ranked: false` and sort last — never hidden, never winning. Comparative
   best/worst claims need two qualifying entries, or the same one is named both.
   `repeat_cancellations()` is deliberately exempt: a flight cancelled every day it was
   scheduled falls below the minimum and would otherwise be invisible.
7. **`is_departure_settled` is not `is_terminal`.** `is_terminal` excludes `DEPARTED`,
   because an airborne flight can still divert. But this system measures *departure*
   reliability, so `DEPARTED` is a settled outcome — the staleness sweep must use
   `is_departure_settled`, or departed flights get falsely flagged as lost tracking.
8. **The staleness sweep is not window-scoped.** Its bound is the stale cutoff, not the
   polled window. A provider's live board drops a flight the moment it departs, so a
   window-scoped sweep could never reach a flight that aged past the lookback — they sat
   at `BOARDING` for days, looking live.

## Timezone contract

- Store UTC (`TIMESTAMPTZ`). Report in `OPERATIONAL_TIMEZONE` (`Europe/Istanbul`).
- Never call `datetime.now()` — use `app.core.timeutil.utcnow()`.
- Never attach a timezone to a naive datetime by guessing; `ensure_utc()` raises on naive
  input deliberately.
- Group analytics by the denormalised local columns (`flight_date_local`,
  `scheduled_hour_local`, `scheduled_dow_local`), not by converting in SQL.
- Use `local_day_bounds()` for day ranges — it is DST-correct.

## Adding a data provider

1. Subclass `FlightDataProvider` in `app/providers/`.
2. Return `NormalizedFlight` objects; use `app/providers/normalization.py` for status and
   delay derivation so rules 3 and 4 hold automatically.
3. Register in `PROVIDER_FACTORIES` (`app/providers/registry.py`).
4. Add settings to `app/core/config.py` **and** `.env.example`.
5. Add a parsing test in `tests/test_providers.py` with a recorded payload.

Do not import a concrete provider anywhere outside `registry.py`.

## Layout

| Path | Responsibility |
|---|---|
| `app/core/` | config, logging, enums, timezone helpers, DB session |
| `app/models/` | SQLAlchemy models |
| `app/providers/` | provider abstraction + implementations + normalization |
| `app/services/collector.py` | the collection cycle |
| `app/analytics/` | `metrics` (pure) → `dimensions` → `aggregator` / `insights` |
| `app/reports/` | `builder` (data) → `render` (text/Telegram) |
| `app/notifications/` | Telegram delivery + alert rules |
| `app/scheduler/` | APScheduler jobs and worker entry point |
| `app/api/routes/` | FastAPI routers |

`analytics/metrics.py` is pure — no database access. Keep it that way; it is what makes
the delay and cancellation rules cheap to test.

## Denominators

Get these wrong and every number lies:

- `cancellation_rate` → over **all** flights.
- `delay_rate`, `on_time_rate` → over **measurable** flights (not cancelled, delay known).
- Flights with unknown delay go to `unknown_flights` and are excluded from both.

## Working commands

```bash
export DATABASE_URL="postgresql+psycopg://flightmon:devpassword@localhost:55432/flightmon"
export TEST_DATABASE_URL="postgresql+psycopg://flightmon:devpassword@localhost:55432/flightmon_test"

.venv/bin/python -m pytest        # 249 tests; integration tests skip without TEST_DATABASE_URL
.venv/bin/ruff check .
.venv/bin/mypy app
.venv/bin/alembic revision --autogenerate -m "description"
.venv/bin/alembic upgrade head
```

Frontend: `npm run typecheck && npm run build` in `frontend/`.

## Conventions

- Type hints everywhere; `mypy app` must stay clean (`disallow_untyped_defs`).
- Structured logging via `get_logger(__name__)`, event-name first:
  `log.info("collection.finished", **summary)`. Never `print()`.
- Comments explain *why*, not *what*. The non-obvious decisions (12-hour AeroDataBox
  window, AeroAPI delays in seconds, denominator choices) are already documented at their
  call sites — keep that up.
- Never hard-code a credential or a default secret.
- Schema changes need an Alembic migration; tests build the schema from migrations, so a
  drifted migration fails the suite.
