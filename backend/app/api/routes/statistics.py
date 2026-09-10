"""Delay, cancellation, time-of-day, airline and trend statistics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.analytics.aggregator import summarise
from app.analytics.insights import (
    rank,
    rank_airlines,
    rank_flights,
    repeat_cancellations,
    time_of_day_verdict,
    trend,
)
from app.analytics.queries import facts_for_local_dates
from app.api.deps import DbSession, Window
from app.api.routes.flights import ROUTE_QUERY_DESCRIPTION, split_route
from app.core.config import settings
from app.core.enums import StatScope
from app.models import HourlyStatistic
from app.schemas.common import RankedOut
from app.schemas.statistics import (
    DailyPointOut,
    HourlyPointOut,
    SummaryOut,
    TimeOfDayOut,
    TrendOut,
)

router = APIRouter(prefix="/statistics", tags=["statistics"])


def _route_filter(
    route: Annotated[str | None, Query(description=ROUTE_QUERY_DESCRIPTION)] = None,
) -> str | None:
    """Validate the ``route`` query parameter every statistics endpoint accepts.

    The value is handed straight to the analytics layer, which has no way to report
    a bad filter to the client, so the ORIGIN-DESTINATION form is enforced once here
    rather than in each endpoint body.
    """
    if route is None:
        return None
    origin, destination = split_route(route)
    return f"{origin}-{destination}"


RouteQ = Annotated[str | None, Depends(_route_filter)]
AirlineQ = Annotated[str | None, Query(description="Airline IATA code.")]


@router.get("", response_model=SummaryOut, summary="Headline statistics")
def statistics(
    session: DbSession,
    window: Window,
    route: RouteQ = None,
    airline: AirlineQ = None,
) -> SummaryOut:
    """Every headline metric for a window, with the reliability score broken down."""
    start, end, label = window
    facts = facts_for_local_dates(session, start, end, route=route, airline=airline)
    payload = summarise(facts)
    return SummaryOut(
        window=label, start_date=start, end_date=end, route=route, airline=airline, **payload
    )


@router.get("/delays", response_model=SummaryOut, summary="Delay statistics")
def delay_statistics(
    session: DbSession,
    window: Window,
    route: RouteQ = None,
    airline: AirlineQ = None,
) -> SummaryOut:
    """Delay distribution and the configured over-threshold buckets.

    ``bucket_percentages`` are shares of *measurable* flights - flights the
    provider gave no timings for are excluded rather than counted as on time.
    """
    return statistics(session, window, route, airline)


@router.get(
    "/cancellations", response_model=SummaryOut, summary="Cancellation statistics"
)
def cancellation_statistics(
    session: DbSession,
    window: Window,
    route: RouteQ = None,
    airline: AirlineQ = None,
) -> SummaryOut:
    """Cancellation counts and rates. The rate denominator is all scheduled flights."""
    return statistics(session, window, route, airline)


@router.get(
    "/hourly",
    response_model=list[HourlyPointOut],
    summary="Metrics by local hour of day",
)
def hourly_statistics(
    session: DbSession,
    window: Window,
    route: RouteQ = None,
    from_cache: Annotated[
        bool,
        Query(
            description="Read materialised hourly_statistics instead of recomputing."
        ),
    ] = True,
) -> list[HourlyPointOut]:
    """Delay and cancellation rates for each of the 24 local hours."""
    start, end, _ = window

    if from_cache:
        scope_key = route.upper() if route else "ALL"
        stmt = select(HourlyStatistic).where(
            HourlyStatistic.stat_date >= start,
            HourlyStatistic.stat_date <= end,
            HourlyStatistic.scope_key == scope_key,
        )
        rows = session.scalars(stmt.order_by(HourlyStatistic.hour_local)).all()
        if rows:
            return _fold_hourly(rows)

    # Fall back to recomputing from facts when the cache is cold.
    facts = facts_for_local_dates(session, start, end, route=route)
    by_hour: dict[int, list] = {hour: [] for hour in range(24)}
    for fact in facts:
        by_hour[fact.hour_local].append(fact)

    out: list[HourlyPointOut] = []
    for hour in range(24):
        payload = summarise(by_hour[hour])
        payload.pop("bucket_percentages", None)
        payload.pop("reliability", None)
        out.append(HourlyPointOut(hour_local=hour, **payload))
    return out


def _fold_hourly(rows: Sequence[HourlyStatistic]) -> list[HourlyPointOut]:
    """Sum cached per-day rows into one point per hour."""
    totals: dict[int, dict[str, float]] = {}
    for row in rows:
        bucket = totals.setdefault(
            row.hour_local,
            {
                "total_flights": 0, "completed_flights": 0, "cancelled_flights": 0,
                "diverted_flights": 0, "delayed_flights": 0, "on_time_flights": 0,
                "unknown_flights": 0, "delayed_gt_15": 0, "delayed_gt_30": 0,
                "delayed_gt_60": 0, "delayed_gt_120": 0, "delay_weight": 0.0,
                "delay_sum": 0.0, "max_delay": 0.0,
            },
        )
        for key in (
            "total_flights", "completed_flights", "cancelled_flights", "diverted_flights",
            "delayed_flights", "on_time_flights", "unknown_flights",
            "delayed_gt_15", "delayed_gt_30", "delayed_gt_60", "delayed_gt_120",
        ):
            bucket[key] += getattr(row, key) or 0
        # Weight each day's mean by the flights behind it so the fold is a true
        # mean, not a mean of means.
        measurable = (row.delayed_flights or 0) + (row.on_time_flights or 0)
        if row.avg_delay_minutes is not None and measurable:
            bucket["delay_sum"] += row.avg_delay_minutes * measurable
            bucket["delay_weight"] += measurable
        if row.max_delay_minutes is not None:
            bucket["max_delay"] = max(bucket["max_delay"], row.max_delay_minutes)

    out: list[HourlyPointOut] = []
    for hour in sorted(totals):
        b = totals[hour]
        measurable = int(b["delayed_flights"] + b["on_time_flights"])
        total = int(b["total_flights"])
        out.append(
            HourlyPointOut(
                hour_local=hour,
                total_flights=total,
                completed_flights=int(b["completed_flights"]),
                cancelled_flights=int(b["cancelled_flights"]),
                diverted_flights=int(b["diverted_flights"]),
                delayed_flights=int(b["delayed_flights"]),
                on_time_flights=int(b["on_time_flights"]),
                unknown_flights=int(b["unknown_flights"]),
                measurable_flights=measurable,
                delayed_gt_15=int(b["delayed_gt_15"]),
                delayed_gt_30=int(b["delayed_gt_30"]),
                delayed_gt_60=int(b["delayed_gt_60"]),
                delayed_gt_120=int(b["delayed_gt_120"]),
                avg_delay_minutes=(
                    round(b["delay_sum"] / b["delay_weight"], 2) if b["delay_weight"] else None
                ),
                max_delay_minutes=int(b["max_delay"]) or None,
                cancellation_rate=(b["cancelled_flights"] / total) if total else None,
                delay_rate=(b["delayed_flights"] / measurable) if measurable else None,
                on_time_rate=(b["on_time_flights"] / measurable) if measurable else None,
                severe_delay_rate=(b["delayed_gt_60"] / measurable) if measurable else None,
            )
        )
    return out


@router.get(
    "/time-of-day",
    response_model=list[TimeOfDayOut],
    summary="Best and worst time-of-day windows",
)
def time_of_day(
    session: DbSession, window: Window, route: RouteQ = None
) -> list[TimeOfDayOut]:
    """Answers "when is the best time to fly?" for each monitored route.

    A period is only eligible to win or lose once it has reached
    ``MIN_SAMPLE_SIZE_PERIOD`` observed flights.
    """
    start, end, _ = window
    facts = facts_for_local_dates(session, start, end)
    routes = [route] if route else settings.route_labels()
    return [
        TimeOfDayOut(**time_of_day_verdict(facts, r).as_dict())  # type: ignore[arg-type]
        for r in routes
    ]


@router.get(
    "/airlines",
    response_model=list[RankedOut],
    summary="Airline reliability ranking",
)
def airline_statistics(
    session: DbSession,
    window: Window,
    route: RouteQ = None,
    ranked_only: Annotated[
        bool, Query(description="Hide airlines below the minimum sample size.")
    ] = False,
) -> list[RankedOut]:
    """Airlines ordered by reliability score, best first.

    Airlines below ``MIN_SAMPLE_SIZE_AIRLINE`` flights are returned with
    ``is_ranked=false`` and sorted last, so a carrier with two lucky flights never
    tops the table.
    """
    start, end, _ = window
    facts = facts_for_local_dates(session, start, end, route=route)
    entries = rank_airlines(facts)
    if ranked_only:
        entries = [e for e in entries if e.is_ranked]
    return [RankedOut(**e.as_dict()) for e in entries]  # type: ignore[arg-type]


@router.get(
    "/flights",
    response_model=list[RankedOut],
    summary="Per-flight-number reliability ranking",
)
def flight_statistics(
    session: DbSession,
    window: Window,
    route: RouteQ = None,
    ranked_only: Annotated[bool, Query()] = False,
) -> list[RankedOut]:
    """Which individual flight numbers are the most and least reliable."""
    start, end, _ = window
    facts = facts_for_local_dates(session, start, end, route=route)
    entries = rank_flights(facts)
    if ranked_only:
        entries = [e for e in entries if e.is_ranked]
    return [RankedOut(**e.as_dict()) for e in entries]  # type: ignore[arg-type]


@router.get(
    "/repeat-cancellations",
    summary="Flight numbers cancelled on multiple days",
)
def repeat_cancellation_report(
    session: DbSession, window: Window, route: RouteQ = None
) -> list[dict[str, object]]:
    """Flights cancelled more than once, whatever their sample size.

    Deliberately exempt from `MIN_SAMPLE_SIZE_FLIGHT`: a flight cancelled every day it
    was scheduled is the most actionable fact in the data, and the ranking threshold
    would hide it entirely.
    """
    start, end, _ = window
    facts = facts_for_local_dates(session, start, end, route=route)
    return repeat_cancellations(facts)


@router.get("/daily", response_model=list[DailyPointOut], summary="Daily time series")
def daily_statistics(
    session: DbSession, window: Window, route: RouteQ = None
) -> list[DailyPointOut]:
    """One point per local day - the series behind the dashboard's trend charts."""
    from collections import defaultdict

    start, end, _ = window
    facts = facts_for_local_dates(session, start, end, route=route)
    by_day: dict = defaultdict(list)
    for fact in facts:
        by_day[fact.flight_date_local].append(fact)

    out: list[DailyPointOut] = []
    for day in sorted(by_day):
        payload = summarise(by_day[day])
        payload.pop("bucket_percentages", None)
        payload.pop("reliability", None)
        out.append(DailyPointOut(stat_date=day, route=route, **payload))
    return out


@router.get(
    "/by/{scope}",
    response_model=list[RankedOut],
    summary="Metrics grouped by any supported dimension",
)
def statistics_by_scope(
    session: DbSession,
    scope: StatScope,
    window: Window,
    route: RouteQ = None,
    ranked_only: Annotated[bool, Query()] = False,
) -> list[RankedOut]:
    """Group the window by one dimension.

    Supported scopes: ``ROUTE``, ``AIRLINE``, ``FLIGHT``, ``PERIOD`` (time-of-day),
    ``DOW`` (ISO day of week, "1".."7"), ``MONTH`` (local "YYYY-MM"),
    ``ROUTE_PERIOD``, ``ROUTE_AIRLINE`` and ``OVERALL``.

    This is what answers "delays by month" or "cancellations by day of week" without a
    dedicated endpoint per dimension.
    """
    start, end, _ = window
    facts = facts_for_local_dates(session, start, end, route=route)
    entries = rank(facts, scope)
    if ranked_only:
        entries = [e for e in entries if e.is_ranked]
    if scope in {StatScope.MONTH, StatScope.DOW, StatScope.PERIOD}:
        # Chronological / natural order reads better than a leaderboard here.
        entries.sort(key=lambda e: e.key)
    return [RankedOut(**e.as_dict()) for e in entries]  # type: ignore[arg-type]


@router.get("/trends", response_model=list[TrendOut], summary="Period-over-period trend")
def trends(
    session: DbSession,
    days: Annotated[int, Query(ge=2, le=365, description="Length of each period.")] = 30,
    route: RouteQ = None,
) -> list[TrendOut]:
    """Compares the last ``days`` days against the ``days`` before them."""
    routes = [route] if route else settings.route_labels()
    return [TrendOut(**trend(session, days, route=r)) for r in routes]  # type: ignore[arg-type]
