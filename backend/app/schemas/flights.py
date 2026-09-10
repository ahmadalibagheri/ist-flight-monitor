"""Flight, observation and event response models."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.enums import DataQuality, EventType, FlightStatus
from app.schemas.common import ORMModel


class ObservationOut(ORMModel):
    """One immutable historical snapshot."""

    id: int
    observed_at: datetime
    status: FlightStatus
    raw_status: str | None = None
    delay_minutes: int | None = None
    is_cancelled: bool
    is_diverted: bool
    estimated_departure_utc: datetime | None = None
    actual_departure_utc: datetime | None = None
    terminal: str | None = None
    gate: str | None = None
    data_source: str
    data_quality: DataQuality


class EventOut(ORMModel):
    id: int
    event_type: EventType
    occurred_at: datetime
    previous_value: str | None = None
    new_value: str | None = None
    detail: str | None = None
    data_source: str


class FlightOut(ORMModel):
    id: int
    flight_number: str
    flight_iata: str | None = None
    airline_iata: str | None = None
    airline_name: str | None = None
    origin_iata: str
    destination_iata: str

    scheduled_departure_utc: datetime
    scheduled_departure_local: str | None = Field(
        None, description="Scheduled departure rendered in the operational timezone."
    )
    first_scheduled_departure_utc: datetime | None = Field(
        None, description="The departure time first advertised, before any re-timing."
    )
    schedule_moved_minutes: int | None = Field(
        None,
        description=(
            "Minutes the advertised departure has shifted since first sighting. "
            "Positive means pushed later."
        ),
    )
    total_displacement_minutes: int | None = Field(
        None,
        description=(
            "delay_minutes plus schedule movement - how much later than originally "
            "advertised the flight left. A re-timed flight can show delay_minutes=0 "
            "and a large displacement."
        ),
    )
    estimated_departure_utc: datetime | None = None
    actual_departure_utc: datetime | None = None
    scheduled_arrival_utc: datetime | None = None
    estimated_arrival_utc: datetime | None = None
    actual_arrival_utc: datetime | None = None

    flight_date_local: date
    scheduled_hour_local: int
    scheduled_dow_local: int

    status: FlightStatus
    delay_minutes: int | None = None
    arrival_delay_minutes: int | None = None
    is_cancelled: bool
    cancellation_reason: str | None = None
    is_diverted: bool

    terminal: str | None = None
    gate: str | None = None
    aircraft_type: str | None = None
    aircraft_registration: str | None = None
    is_codeshare: bool

    data_source: str
    data_quality: DataQuality
    is_mock: bool
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int


class FlightDetailOut(FlightOut):
    """A flight together with its full observation and event history."""

    observations: list[ObservationOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)


class FlightHistoryOut(BaseModel):
    """All observed instances of one flight number, plus its aggregate metrics."""

    flight_number: str
    flights: list[FlightOut]
    total_observed: int
