"""Loading :class:`FlightFact` rows out of PostgreSQL.

Every query in this module applies two filters without exception:

* ``Flight.is_mock == False`` - synthetic development data never reaches analytics.
* ``DataQuality != UNRELIABLE`` - contradictory timings are excluded rather than
  averaged in.

Both live in :func:`_base_select` so a new query cannot forget them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.analytics.metrics import FlightFact
from app.core.config import settings
from app.core.enums import DataQuality, FlightStatus
from app.core.timeutil import local_day_bounds, utcnow
from app.models import Flight


def _base_select() -> Select[tuple[Flight]]:
    return select(Flight).where(
        Flight.is_mock.is_(False),
        Flight.data_quality != DataQuality.UNRELIABLE,
    )


def _to_fact(flight: Flight) -> FlightFact:
    return FlightFact(
        flight_id=flight.id,
        flight_number=flight.flight_number,
        airline_iata=flight.airline_iata,
        airline_icao=flight.airline_icao,
        airline_name=flight.airline_name,
        origin_iata=flight.origin_iata,
        destination_iata=flight.destination_iata,
        scheduled_departure_utc=flight.scheduled_departure_utc,
        flight_date_local=flight.flight_date_local,
        hour_local=flight.scheduled_hour_local,
        dow_local=flight.scheduled_dow_local,
        status=flight.status,
        delay_minutes=flight.delay_minutes,
        is_cancelled=flight.is_cancelled,
        is_diverted=flight.is_diverted,
    )


def facts_for_local_dates(
    session: Session,
    start: date,
    end: date,
    *,
    route: str | None = None,
    airline: str | None = None,
    flight_number: str | None = None,
    completed_only: bool = False,
) -> list[FlightFact]:
    """Facts for local calendar dates ``[start, end]``, inclusive on both ends."""
    stmt = _base_select().where(
        Flight.flight_date_local >= start,
        Flight.flight_date_local <= end,
    )
    stmt = _apply_filters(stmt, route, airline, flight_number)
    if completed_only:
        stmt = stmt.where(
            Flight.status.in_(
                [
                    FlightStatus.DEPARTED,
                    FlightStatus.ARRIVED,
                    FlightStatus.CANCELLED,
                    FlightStatus.DIVERTED,
                ]
            )
        )
    return [_to_fact(f) for f in session.scalars(stmt).all()]


def facts_for_window(
    session: Session,
    window_start: datetime,
    window_end: datetime,
    *,
    route: str | None = None,
    airline: str | None = None,
) -> list[FlightFact]:
    """Facts whose scheduled departure falls in a UTC half-open window."""
    stmt = _base_select().where(
        Flight.scheduled_departure_utc >= window_start,
        Flight.scheduled_departure_utc < window_end,
    )
    stmt = _apply_filters(stmt, route, airline, None)
    return [_to_fact(f) for f in session.scalars(stmt).all()]


def facts_for_today(session: Session, *, route: str | None = None) -> list[FlightFact]:
    """Facts for the current *local* operational day."""
    today = local_today()
    start, end = local_day_bounds(today)
    return facts_for_window(session, start, end, route=route)


def facts_rolling(
    session: Session,
    days: int,
    *,
    route: str | None = None,
    airline: str | None = None,
    reference: date | None = None,
) -> list[FlightFact]:
    """Facts for a rolling window ending on (and including) ``reference``."""
    end = reference or local_today()
    start = end - timedelta(days=days - 1)
    return facts_for_local_dates(session, start, end, route=route, airline=airline)


def flights_for_local_day(
    session: Session, day: date, *, route: str | None = None
) -> list[Flight]:
    """Full ORM rows for a local day - used by reports that need gate/terminal."""
    start, end = local_day_bounds(day)
    stmt = (
        _base_select()
        .where(
            Flight.scheduled_departure_utc >= start,
            Flight.scheduled_departure_utc < end,
        )
        .order_by(Flight.scheduled_departure_utc)
    )
    stmt = _apply_filters(stmt, route, None, None)
    return list(session.scalars(stmt).all())


def local_today() -> date:
    """Today's date in the operational timezone."""
    from app.core.timeutil import to_local

    now_local = to_local(utcnow())
    assert now_local is not None
    return now_local.date()


def _apply_filters(
    stmt: Select[tuple[Flight]],
    route: str | None,
    airline: str | None,
    flight_number: str | None,
) -> Select[tuple[Flight]]:
    if route:
        origin, _, destination = route.partition("-")
        if destination:
            stmt = stmt.where(
                and_(
                    Flight.origin_iata == origin.upper(),
                    Flight.destination_iata == destination.upper(),
                )
            )
        else:
            # Bare destination code, e.g. "IKA".
            stmt = stmt.where(
                and_(
                    Flight.origin_iata == settings.origin_airport,
                    Flight.destination_iata == route.upper(),
                )
            )
    if airline:
        stmt = stmt.where(Flight.airline_iata == airline.upper())
    if flight_number:
        stmt = stmt.where(Flight.flight_number == flight_number.replace(" ", "").upper())
    return stmt
