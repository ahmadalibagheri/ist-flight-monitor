"""Assembles report data structures from the analytics layer.

Two report shapes are produced:

* :func:`build_hourly_report` - "what is happening right now", plus the
  time-of-day and reliability verdicts that only make sense over a longer window.
* :func:`build_historical_report` - a rolling-window retrospective.

A deliberate choice worth stating: the *current status* block counts today's
flights, but the *time analysis* and *reliability* blocks are computed over the
rolling history window. A single day never contains enough flights to say
"18:00-22:00 is the worst time to fly" - claiming that from one day of data would
be noise dressed up as insight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.analytics.insights import (
    PeriodVerdict,
    RankedEntry,
    rank_airlines,
    rank_flights,
    repeat_cancellations,
    time_of_day_verdict,
    trend,
)
from app.analytics.metrics import MetricBlock, compute_metrics
from app.analytics.queries import (
    facts_for_local_dates,
    flights_for_local_day,
    local_today,
)
from app.analytics.reliability import compute_reliability
from app.core.config import settings
from app.core.enums import FlightStatus
from app.core.logging_config import get_logger
from app.core.timeutil import to_local, utcnow
from app.models import Flight

log = get_logger(__name__)


@dataclass(slots=True)
class FlightIssue:
    """A flight the report needs to call out by name."""

    flight_number: str
    airline: str
    scheduled_local: str
    destination: str
    status: str
    delay_minutes: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "flight_number": self.flight_number,
            "airline": self.airline,
            "scheduled_local": self.scheduled_local,
            "destination": self.destination,
            "status": self.status,
            "delay_minutes": self.delay_minutes,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RouteBlock:
    """Today's numbers for one monitored route."""

    route: str
    destination_name: str
    metrics: MetricBlock
    departed: int = 0
    scheduled_remaining: int = 0

    def as_dict(self) -> dict[str, object]:
        payload = dict(self.metrics.as_columns())
        payload.update(
            {
                "route": self.route,
                "destination_name": self.destination_name,
                "departed": self.departed,
                "scheduled_remaining": self.scheduled_remaining,
                "measurable_flights": self.metrics.measurable_flights,
            }
        )
        return payload


@dataclass(slots=True)
class HourlyReport:
    generated_at_utc: datetime
    generated_at_local: str
    local_date: date
    history_days: int
    routes: list[RouteBlock] = field(default_factory=list)
    cancelled: list[FlightIssue] = field(default_factory=list)
    delayed: list[FlightIssue] = field(default_factory=list)
    period_verdicts: list[PeriodVerdict] = field(default_factory=list)
    best_flight: RankedEntry | None = None
    worst_flight: RankedEntry | None = None
    #: Flight numbers cancelled on multiple days in the history window. Surfaced in
    #: the report because it is the most actionable thing the data knows, and it is
    #: invisible in the ranking (those flights sit below the sample-size minimum).
    repeat_cancellations: list[dict[str, object]] = field(default_factory=list)
    data_note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "generated_at_local": self.generated_at_local,
            "local_date": self.local_date.isoformat(),
            "history_days": self.history_days,
            "routes": [r.as_dict() for r in self.routes],
            "cancelled": [c.as_dict() for c in self.cancelled],
            "delayed": [d.as_dict() for d in self.delayed],
            "period_verdicts": [p.as_dict() for p in self.period_verdicts],
            "best_flight": self.best_flight.as_dict() if self.best_flight else None,
            "worst_flight": self.worst_flight.as_dict() if self.worst_flight else None,
            "repeat_cancellations": self.repeat_cancellations,
            "data_note": self.data_note,
        }


@dataclass(slots=True)
class HistoricalReport:
    generated_at_utc: datetime
    generated_at_local: str
    days: int
    start_date: date
    end_date: date
    routes: list[dict[str, object]] = field(default_factory=list)
    airlines: list[dict[str, object]] = field(default_factory=list)
    overall: dict[str, object] = field(default_factory=dict)
    trends: list[dict[str, object]] = field(default_factory=list)
    data_note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "generated_at_local": self.generated_at_local,
            "days": self.days,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "overall": self.overall,
            "routes": self.routes,
            "airlines": self.airlines,
            "trends": self.trends,
            "data_note": self.data_note,
        }


ROUTE_NAMES: dict[str, str] = {"IKA": "TEHRAN", "MHD": "MASHHAD"}


def build_hourly_report(
    session: Session, *, history_days: int | None = None
) -> HourlyReport:
    """Assemble the hourly operational report."""
    now = utcnow()
    now_local = to_local(now)
    assert now_local is not None
    today = local_today()
    window = history_days or settings.historical_report_days

    report = HourlyReport(
        generated_at_utc=now,
        generated_at_local=now_local.strftime("%Y-%m-%d %H:%M"),
        local_date=today,
        history_days=window,
    )

    history = facts_for_local_dates(session, today - timedelta(days=window - 1), today)
    todays_flights = flights_for_local_day(session, today)

    if not history and not todays_flights:
        report.data_note = (
            "No real flight data collected yet. Configure a provider "
            "(see docs/PROVIDERS.md) and wait for the first collection cycle."
        )

    # ---- per-route current status ---------------------------------------
    for destination in settings.destination_airports:
        route = f"{settings.origin_airport}-{destination}"
        route_flights = [f for f in todays_flights if f.destination_iata == destination]
        facts = facts_for_local_dates(session, today, today, route=route)
        block = RouteBlock(
            route=route,
            destination_name=ROUTE_NAMES.get(destination, destination),
            metrics=compute_metrics(facts),
            departed=sum(1 for f in route_flights if f.status.has_departed),
            scheduled_remaining=sum(
                1
                for f in route_flights
                if f.status in {FlightStatus.SCHEDULED, FlightStatus.BOARDING, FlightStatus.DELAYED}
            ),
        )
        report.routes.append(block)

    # ---- current issues --------------------------------------------------
    for flight in todays_flights:
        if flight.is_cancelled:
            report.cancelled.append(_issue(flight))
        elif (
            flight.delay_minutes is not None
            and flight.delay_minutes >= settings.delay_threshold_minutes
            and not flight.status.has_departed
        ):
            report.delayed.append(_issue(flight))
    report.delayed.sort(key=lambda i: i.delay_minutes or 0, reverse=True)

    # ---- longer-window verdicts -----------------------------------------
    for destination in settings.destination_airports:
        route = f"{settings.origin_airport}-{destination}"
        report.period_verdicts.append(time_of_day_verdict(history, route))

    report.repeat_cancellations = repeat_cancellations(history)

    flight_ranking = [entry for entry in rank_flights(history) if entry.is_ranked]
    if flight_ranking:
        report.best_flight = flight_ranking[0]
        report.worst_flight = flight_ranking[-1]

    return report


def build_historical_report(
    session: Session, *, days: int | None = None
) -> HistoricalReport:
    """Assemble the rolling-window retrospective."""
    now = utcnow()
    now_local = to_local(now)
    assert now_local is not None
    window = days or settings.historical_report_days
    end = local_today()
    start = end - timedelta(days=window - 1)

    facts = facts_for_local_dates(session, start, end)
    report = HistoricalReport(
        generated_at_utc=now,
        generated_at_local=now_local.strftime("%Y-%m-%d %H:%M"),
        days=window,
        start_date=start,
        end_date=end,
    )

    if not facts:
        report.data_note = (
            f"No real flight data in the last {window} days. "
            "Historical statistics need at least one completed collection cycle."
        )
        return report

    overall = compute_metrics(facts)
    overall.reliability_score = compute_reliability(overall, min_sample_size=1).score
    report.overall = dict(overall.as_columns())

    for destination in settings.destination_airports:
        route = f"{settings.origin_airport}-{destination}"
        route_facts = [f for f in facts if f.route == route]
        block = compute_metrics(route_facts)
        block.reliability_score = compute_reliability(block, min_sample_size=1).score
        verdict = time_of_day_verdict(facts, route)
        payload = dict(block.as_columns())
        payload.update(
            {
                "route": route,
                "destination_name": ROUTE_NAMES.get(destination, destination),
                "best_period": verdict.best_period,
                "worst_delay_period": verdict.worst_delay_period,
                "worst_cancellation_period": verdict.worst_cancellation_period,
                "period_note": verdict.note,
            }
        )
        report.routes.append(payload)
        report.trends.append(trend(session, window, route=route))

    report.airlines = [entry.as_dict() for entry in rank_airlines(facts)]
    return report


def _issue(flight: Flight) -> FlightIssue:
    local = to_local(flight.scheduled_departure_utc)
    return FlightIssue(
        flight_number=flight.flight_number,
        airline=flight.airline_name or flight.airline_iata or "Unknown",
        scheduled_local=local.strftime("%H:%M") if local else "??:??",
        destination=flight.destination_iata,
        status=flight.status.value,
        delay_minutes=flight.delay_minutes,
        reason=flight.cancellation_reason,
    )
