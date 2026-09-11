"""Flight identity, append-only observations and derived events.

Storage model
-------------
``flights``             one row per operating flight-leg on a local calendar day.
                        Carries the *latest known* state, denormalised so the
                        dashboard and reports do not have to re-derive it.
``flight_observations``  append-only. One row per flight per collection cycle.
                        Never updated, never deleted - this is the history that
                        every long-term statistic is computed from.
``flight_events``        derived deltas between consecutive observations. Drives
                        alerting and gives a compact audit trail.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.enums import DataQuality, EventType, FlightStatus
from app.core.timeutil import utcnow

StatusEnum = SAEnum(FlightStatus, name="flight_status", native_enum=True, validate_strings=True)
EventEnum = SAEnum(EventType, name="flight_event_type", native_enum=True, validate_strings=True)
QualityEnum = SAEnum(DataQuality, name="data_quality", native_enum=True, validate_strings=True)


class Flight(Base):
    """A single operating flight leg on one local calendar day."""

    __tablename__ = "flights"
    __table_args__ = (
        UniqueConstraint(
            "flight_number",
            "origin_iata",
            "destination_iata",
            "flight_date_local",
            name="uq_flights_identity",
        ),
        CheckConstraint(
            "delay_minutes IS NULL OR delay_minutes >= -180",
            name="delay_minutes_sane",
        ),
        Index("ix_flights_route_sched", "origin_iata", "destination_iata", "scheduled_departure_utc"),
        Index("ix_flights_sched_dep", "scheduled_departure_utc"),
        Index("ix_flights_flight_number", "flight_number"),
        Index("ix_flights_airline_status", "airline_iata", "status"),
        Index("ix_flights_status", "status"),
        Index("ix_flights_date_local", "flight_date_local"),
        Index("ix_flights_local_hour", "scheduled_hour_local"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # ---- identity -------------------------------------------------------
    flight_number: Mapped[str] = mapped_column(String(10), nullable=False)
    flight_iata: Mapped[str | None] = mapped_column(String(10), index=True)
    flight_icao: Mapped[str | None] = mapped_column(String(10))
    airline_iata: Mapped[str | None] = mapped_column(String(3), index=True)
    airline_icao: Mapped[str | None] = mapped_column(String(4))
    airline_name: Mapped[str | None] = mapped_column(String(160))
    origin_iata: Mapped[str] = mapped_column(String(3), nullable=False)
    destination_iata: Mapped[str] = mapped_column(String(3), nullable=False)

    # ---- schedule (UTC storage, local projections for analytics) --------
    scheduled_departure_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: The departure time first advertised for this flight, captured once and never
    #: updated.
    #:
    #: Airlines re-time flights, and providers overwrite ``scheduledTime`` when they
    #: do. Delay measured against the *current* schedule then reads ~0 no matter how
    #: far the flight moved - a five-hour retime scores as perfectly on time. Keeping
    #: the original makes that displacement measurable instead of invisible.
    first_scheduled_departure_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Denormalised local projections. Written by the collector so that grouping
    # queries never pay for an AT TIME ZONE conversion or an index miss.
    flight_date_local: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_hour_local: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    scheduled_dow_local: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    estimated_departure_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_departure_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- latest known state --------------------------------------------
    status: Mapped[FlightStatus] = mapped_column(
        StatusEnum, nullable=False, default=FlightStatus.UNKNOWN
    )
    #: Departure delay in minutes. NULL means "not enough data to say" - it is
    #: never coerced to 0, because an unknown delay is not an on-time flight.
    delay_minutes: Mapped[int | None] = mapped_column(Integer)
    arrival_delay_minutes: Mapped[int | None] = mapped_column(Integer)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    is_diverted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    terminal: Mapped[str | None] = mapped_column(String(16))
    gate: Mapped[str | None] = mapped_column(String(16))
    aircraft_type: Mapped[str | None] = mapped_column(String(64))
    aircraft_registration: Mapped[str | None] = mapped_column(String(16))

    is_codeshare: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    codeshare_of: Mapped[str | None] = mapped_column(String(10))

    # ---- provenance -----------------------------------------------------
    #: In merge mode this is every contributing provider, e.g. "aerodatabox+opensky".
    data_source: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Which provider supplied each field, when several were merged. Without this a
    #: merged row is unauditable - you cannot tell whose gate number you are showing.
    field_sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: True when *every* observation came from a MOCK provider. Analytics
    #: excludes these rows so development data can never pollute statistics.
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    data_quality: Mapped[DataQuality] = mapped_column(
        QualityEnum, nullable=False, default=DataQuality.OK
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    observations: Mapped[list[FlightObservation]] = relationship(
        back_populates="flight",
        cascade="all, delete-orphan",
        order_by="FlightObservation.observed_at",
        lazy="selectin",
    )
    events: Mapped[list[FlightEvent]] = relationship(
        back_populates="flight",
        cascade="all, delete-orphan",
        order_by="FlightEvent.occurred_at",
    )

    @property
    def route(self) -> str:
        return f"{self.origin_iata}-{self.destination_iata}"

    #: How long after its expected departure a non-terminal status stops being
    #: credible. Boarding legitimately continues up to departure and a little past
    #: it, so a short grace avoids flagging a flight that is mid-departure.
    UNRESOLVED_AFTER_MINUTES = 120

    @property
    def expected_departure_utc(self) -> datetime:
        """The latest time the flight was expected to leave.

        A status must be judged against this rather than the original schedule: a
        flight delayed two hours is not overdue at its original time. The provider's
        own revised estimate is preferred over ``scheduled + delay_minutes`` because
        it is the value the delay was derived *from*, so it stays correct even when
        the derived figure is missing.
        """
        if self.estimated_departure_utc is not None:
            return max(self.estimated_departure_utc, self.scheduled_departure_utc)
        if self.delay_minutes is not None and self.delay_minutes > 0:
            return self.scheduled_departure_utc + timedelta(minutes=self.delay_minutes)
        return self.scheduled_departure_utc

    @property
    def outcome_unresolved(self) -> bool:
        """True when the departure is long past but no outcome ever arrived.

        Distinct from :attr:`DataQuality.STALE`, and both can hold at once. Staleness
        is about *our polling* - how long since the provider mentioned this flight.
        This is about *the flight's own timeline*: a status of BOARDING sixteen hours
        after the aircraft was due to leave is not a live status, whatever the polling
        interval is, and a board that presents it as current is misleading.
        """
        if self.status.is_departure_settled:
            return False
        overdue = utcnow() - self.expected_departure_utc
        return overdue > timedelta(minutes=self.UNRESOLVED_AFTER_MINUTES)

    @property
    def schedule_moved_minutes(self) -> int | None:
        """Minutes the advertised departure has shifted since first sighting.

        Positive means the flight was pushed later. ``None`` when the original is
        unknown (rows created before this was tracked).
        """
        if self.first_scheduled_departure_utc is None:
            return None
        delta = self.scheduled_departure_utc - self.first_scheduled_departure_utc
        return int(delta.total_seconds() // 60)

    @property
    def total_displacement_minutes(self) -> int | None:
        """Departure delay *plus* schedule movement - what a passenger experienced.

        ``delay_minutes`` alone answers "did it leave when the airline last said?".
        This answers "how much later than originally advertised did it leave?", which
        is the question a retime would otherwise hide.
        """
        moved = self.schedule_moved_minutes
        if moved is None and self.delay_minutes is None:
            return None
        return (moved or 0) + (self.delay_minutes or 0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Flight {self.flight_number} {self.route} {self.flight_date_local} {self.status}>"


class FlightObservation(Base):
    """One immutable snapshot of a flight as reported at ``observed_at``."""

    __tablename__ = "flight_observations"
    __table_args__ = (
        # Two providers may legitimately report the same flight in one cycle;
        # a re-poll from the *same* provider in the same cycle must not duplicate.
        UniqueConstraint(
            "flight_id", "collection_run_id", "data_source", name="uq_observation_per_run"
        ),
        Index("ix_obs_flight_time", "flight_id", "observed_at"),
        Index("ix_obs_observed_at", "observed_at"),
        Index("ix_obs_status", "status"),
        Index("ix_obs_source", "data_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("flights.id", ondelete="CASCADE"), nullable=False
    )
    collection_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("collection_runs.id", ondelete="SET NULL")
    )

    #: When the collector recorded this snapshot (UTC).
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[FlightStatus] = mapped_column(StatusEnum, nullable=False)
    raw_status: Mapped[str | None] = mapped_column(String(64))
    delay_minutes: Mapped[int | None] = mapped_column(Integer)
    arrival_delay_minutes: Mapped[int | None] = mapped_column(Integer)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    is_diverted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    scheduled_departure_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_departure_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_departure_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    terminal: Mapped[str | None] = mapped_column(String(16))
    gate: Mapped[str | None] = mapped_column(String(16))
    aircraft_type: Mapped[str | None] = mapped_column(String(64))
    aircraft_registration: Mapped[str | None] = mapped_column(String(16))

    data_source: Mapped[str] = mapped_column(String(40), nullable=False)
    data_quality: Mapped[DataQuality] = mapped_column(
        QualityEnum, nullable=False, default=DataQuality.OK
    )

    #: The provider's response for this flight, stored verbatim.
    #:
    #: Historical observations can never be re-fetched - a provider will not tell you
    #: what it said about a flight last Tuesday. Anything not modelled as a column is
    #: therefore lost forever the moment it is discarded. Keeping the raw payload is
    #: cheap insurance (~1 KB per observation) and means a field we did not think to
    #: model today is still recoverable from history tomorrow.
    #:
    #: Disable with STORE_RAW_PAYLOAD=false if storage is constrained.
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    flight: Mapped[Flight] = relationship(back_populates="observations")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Observation flight={self.flight_id} at={self.observed_at} {self.status}>"


class FlightEvent(Base):
    """A meaningful change between two consecutive observations."""

    __tablename__ = "flight_events"
    __table_args__ = (
        Index("ix_events_flight_time", "flight_id", "occurred_at"),
        Index("ix_events_type_time", "event_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("flights.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[EventType] = mapped_column(EventEnum, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_value: Mapped[str | None] = mapped_column(String(120))
    new_value: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text)
    data_source: Mapped[str] = mapped_column(String(40), nullable=False)

    flight: Mapped[Flight] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Event {self.event_type} flight={self.flight_id}>"
