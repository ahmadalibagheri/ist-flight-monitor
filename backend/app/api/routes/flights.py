"""Flight listing, detail and history."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, func, select

from app.api.deps import DbSession, Pagination
from app.core.config import settings
from app.core.enums import FlightStatus
from app.core.timeutil import to_local
from app.models import Airline, Airport, Flight
from app.schemas.common import AirlineOut, Page, RouteOut
from app.schemas.flights import FlightDetailOut, FlightHistoryOut, FlightOut

router = APIRouter(tags=["flights"])


def _serialise(flight: Flight) -> FlightOut:
    out = FlightOut.model_validate(flight)
    local = to_local(flight.scheduled_departure_utc)
    out.scheduled_departure_local = local.strftime("%Y-%m-%d %H:%M") if local else None
    out.schedule_moved_minutes = flight.schedule_moved_minutes
    out.total_displacement_minutes = flight.total_displacement_minutes
    return out


#: Wording of the ``route`` query parameter, shared with the statistics endpoints.
ROUTE_QUERY_DESCRIPTION = (
    'Full route as ORIGIN-DESTINATION, e.g. "IST-IKA". A bare airport code is '
    "rejected because it does not name a direction."
)


def split_route(route: str) -> tuple[str, str]:
    """Split a ``route`` filter into ``(origin, destination)``.

    A bare code used to be read as a destination reached from the single configured
    origin. With return legs monitored an airport is both an origin and a
    destination, so ``IKA`` names two opposite routes; answering one of them
    silently would be worse than refusing the filter.
    """
    origin, separator, destination = route.strip().upper().partition("-")
    if not separator or not origin or not destination:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Route filter '{route}' must be written as ORIGIN-DESTINATION. "
                f"Monitored routes: {', '.join(settings.route_labels())}."
            ),
        )
    return origin, destination


def _filtered(
    route: str | None,
    airline: str | None,
    flight_number: str | None,
    flight_status: FlightStatus | None,
    start_date: date | None,
    end_date: date | None,
    include_mock: bool,
) -> Select[tuple[Flight]]:
    stmt = select(Flight)
    if not include_mock:
        stmt = stmt.where(Flight.is_mock.is_(False))
    if route:
        origin, destination = split_route(route)
        stmt = stmt.where(
            Flight.origin_iata == origin,
            Flight.destination_iata == destination,
        )
    if airline:
        stmt = stmt.where(Flight.airline_iata == airline.upper())
    if flight_number:
        stmt = stmt.where(Flight.flight_number == flight_number.replace(" ", "").upper())
    if flight_status:
        stmt = stmt.where(Flight.status == flight_status)
    if start_date:
        stmt = stmt.where(Flight.flight_date_local >= start_date)
    if end_date:
        stmt = stmt.where(Flight.flight_date_local <= end_date)
    return stmt


@router.get("/flights", response_model=Page[FlightOut], summary="List flights")
def list_flights(
    session: DbSession,
    page: Pagination,
    route: Annotated[str | None, Query(description=ROUTE_QUERY_DESCRIPTION)] = None,
    airline: Annotated[str | None, Query(description="Airline IATA code.")] = None,
    flight_number: Annotated[str | None, Query()] = None,
    flight_status: Annotated[FlightStatus | None, Query(alias="status")] = None,
    start_date: Annotated[date | None, Query(description="Local date, inclusive.")] = None,
    end_date: Annotated[date | None, Query(description="Local date, inclusive.")] = None,
    include_mock: Annotated[
        bool, Query(description="Include synthetic development data. Default false.")
    ] = False,
) -> Page[FlightOut]:
    """Flights matching the filters, most recent scheduled departure first."""
    limit, offset = page
    stmt = _filtered(
        route, airline, flight_number, flight_status, start_date, end_date, include_mock
    )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(Flight.scheduled_departure_utc.desc()).limit(limit).offset(offset)
    ).all()
    return Page[FlightOut](
        items=[_serialise(f) for f in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


@router.get(
    "/flights/{flight_id:int}",
    response_model=FlightDetailOut,
    summary="One flight with its full observation history",
)
def get_flight(session: DbSession, flight_id: int) -> FlightDetailOut:
    flight = session.get(Flight, flight_id)
    if flight is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Flight not found")
    detail = FlightDetailOut.model_validate(flight)
    local = to_local(flight.scheduled_departure_utc)
    detail.scheduled_departure_local = local.strftime("%Y-%m-%d %H:%M") if local else None
    detail.events = flight.events  # type: ignore[assignment]
    return detail


@router.get(
    "/flights/{flight_number}",
    response_model=FlightHistoryOut,
    summary="Every observed instance of a flight number",
)
def flight_history(
    session: DbSession,
    flight_number: str,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
) -> FlightHistoryOut:
    """All dated instances of e.g. ``TK878``, newest first."""
    normalised = flight_number.replace(" ", "").upper()
    stmt = _filtered(None, None, normalised, None, start_date, end_date, False)
    rows = session.scalars(stmt.order_by(Flight.scheduled_departure_utc.desc())).all()
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No flights recorded for '{normalised}'",
        )
    return FlightHistoryOut(
        flight_number=normalised,
        flights=[_serialise(f) for f in rows],
        total_observed=len(rows),
    )


@router.get("/routes", response_model=list[RouteOut], summary="Monitored routes")
def list_routes(session: DbSession) -> list[RouteOut]:
    """Configured routes, annotated with what has actually been observed."""
    airports = {a.iata: a for a in session.scalars(select(Airport)).all()}
    out: list[RouteOut] = []
    for origin, destination in settings.routes:
        stats = session.execute(
            select(
                func.count(Flight.id),
                func.min(Flight.flight_date_local),
                func.max(Flight.flight_date_local),
            ).where(
                Flight.origin_iata == origin,
                Flight.destination_iata == destination,
                Flight.is_mock.is_(False),
            )
        ).one()
        from_airport = airports.get(origin)
        to_airport = airports.get(destination)
        out.append(
            RouteOut(
                route=f"{origin}-{destination}",
                origin_iata=origin,
                destination_iata=destination,
                origin_name=from_airport.name if from_airport else None,
                origin_city=from_airport.city if from_airport else None,
                destination_name=to_airport.name if to_airport else None,
                destination_city=to_airport.city if to_airport else None,
                total_flights=stats[0] or 0,
                first_seen=stats[1],
                last_seen=stats[2],
            )
        )
    return out


@router.get("/airlines", response_model=list[AirlineOut], summary="Discovered airlines")
def list_airlines(session: DbSession) -> list[AirlineOut]:
    """Airlines learned from provider payloads, not a hand-maintained list."""
    rows = session.scalars(select(Airline).order_by(Airline.name)).all()
    return [AirlineOut.model_validate(a) for a in rows]
