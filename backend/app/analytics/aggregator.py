"""Materialises ``daily_statistics`` and ``hourly_statistics``.

These tables are a *cache*. They are recomputed idempotently from
``flights``/``flight_observations`` and can be dropped and rebuilt at any time -
:func:`rebuild_range` does exactly that. The API reads them so a dashboard load
never triggers a full scan of the observation history.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analytics.dimensions import metrics_by_scope, route_for_scope
from app.analytics.metrics import FlightFact, compute_metrics
from app.analytics.queries import facts_for_local_dates, local_today
from app.analytics.reliability import compute_reliability
from app.core.enums import StatScope
from app.core.logging_config import get_logger
from app.models import DailyStatistic, HourlyStatistic

log = get_logger(__name__)

#: Scopes materialised into ``daily_statistics``.
DAILY_SCOPES: tuple[StatScope, ...] = (
    StatScope.OVERALL,
    StatScope.ROUTE,
    StatScope.AIRLINE,
    StatScope.FLIGHT,
    StatScope.PERIOD,
    StatScope.DOW,
    StatScope.MONTH,
    StatScope.ROUTE_PERIOD,
    StatScope.ROUTE_AIRLINE,
)

#: Hourly granularity is only worth storing for the coarse scopes; per-flight
#: hourly rows would be one row per flight and carry no extra information.
HOURLY_SCOPES: tuple[StatScope, ...] = (
    StatScope.OVERALL,
    StatScope.ROUTE,
    StatScope.AIRLINE,
)


def aggregate_day(session: Session, day: date) -> dict[str, int]:
    """(Re)compute every statistic row for one local calendar day."""
    facts = facts_for_local_dates(session, day, day)

    daily_written = _write_daily(session, day, facts)
    hourly_written = _write_hourly(session, day, facts)

    session.flush()
    result = {
        "date": day.isoformat(),
        "flights": len(facts),
        "daily_rows": daily_written,
        "hourly_rows": hourly_written,
    }
    log.info("analytics.aggregated", **result)
    return result  # type: ignore[return-value]


def aggregate_recent(session: Session, days: int = 2) -> list[dict[str, int]]:
    """Refresh the last ``days`` local days - today plus the tail that may still move.

    Yesterday is re-aggregated because late-arriving actual times keep changing a
    day's numbers for several hours after local midnight.
    """
    today = local_today()
    return [
        aggregate_day(session, today - timedelta(days=offset))
        for offset in range(days)
    ]


def rebuild_range(session: Session, start: date, end: date) -> list[dict[str, int]]:
    """Drop and rebuild statistics across an inclusive local date range."""
    session.execute(
        delete(DailyStatistic).where(
            DailyStatistic.stat_date >= start, DailyStatistic.stat_date <= end
        )
    )
    session.execute(
        delete(HourlyStatistic).where(
            HourlyStatistic.stat_date >= start, HourlyStatistic.stat_date <= end
        )
    )
    out: list[dict[str, int]] = []
    day = start
    while day <= end:
        out.append(aggregate_day(session, day))
        day += timedelta(days=1)
    return out


# ------------------------------------------------------------------- internals
def _write_daily(session: Session, day: date, facts: list[FlightFact]) -> int:
    existing = {
        (row.scope, row.scope_key): row
        for row in session.scalars(
            select(DailyStatistic).where(DailyStatistic.stat_date == day)
        ).all()
    }
    written = 0
    for scope in DAILY_SCOPES:
        for key, block in metrics_by_scope(facts, scope).items():
            row = existing.get((scope, key))
            if row is None:
                row = DailyStatistic(stat_date=day, scope=scope, scope_key=key)
                session.add(row)
            row.route = route_for_scope(scope, key)
            for column, value in block.as_columns().items():
                setattr(row, column, value)
            written += 1
    return written


def _write_hourly(session: Session, day: date, facts: list[FlightFact]) -> int:
    by_hour: dict[int, list[FlightFact]] = defaultdict(list)
    for fact in facts:
        by_hour[fact.hour_local].append(fact)

    existing = {
        (row.hour_local, row.scope, row.scope_key): row
        for row in session.scalars(
            select(HourlyStatistic).where(HourlyStatistic.stat_date == day)
        ).all()
    }

    written = 0
    for hour, hour_facts in by_hour.items():
        for scope in HOURLY_SCOPES:
            for key, block in metrics_by_scope(hour_facts, scope).items():
                row = existing.get((hour, scope, key))
                if row is None:
                    row = HourlyStatistic(
                        stat_date=day, hour_local=hour, scope=scope, scope_key=key
                    )
                    session.add(row)
                row.route = route_for_scope(scope, key)
                for column, value in block.as_columns().items():
                    setattr(row, column, value)
                written += 1
    return written


def summarise(facts: list[FlightFact], *, min_sample_size: int | None = None) -> dict[str, object]:
    """One metric block plus its score, shaped for API and report consumption."""
    block = compute_metrics(facts, min_sample_size=min_sample_size)
    score = compute_reliability(block, min_sample_size=min_sample_size)
    # compute_metrics leaves reliability_score unset; fill it so the flat field and
    # the nested breakdown can never disagree.
    block.reliability_score = score.score
    payload = dict(block.as_columns())
    payload["measurable_flights"] = block.measurable_flights
    payload["bucket_percentages"] = {
        str(k): round(v, 4) for k, v in block.bucket_percentages.items()
    }
    payload["sample_warning"] = block.sample_warning
    payload["reliability"] = score.as_dict()
    return payload
