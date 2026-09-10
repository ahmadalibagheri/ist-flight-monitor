"""Collector integration tests: append-only history, event derivation, staleness.

These need a real PostgreSQL because the append-only guarantee is enforced by a
unique constraint, and because the enum columns are native database types.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.enums import DataQuality, EventType, FlightStatus
from app.models import Airline, Flight, FlightEvent, FlightObservation
from app.providers.base import NormalizedFlight, ProviderResult
from app.providers.registry import ProviderRegistry
from app.services.collector import FlightCollector, _deduplicate
from tests.conftest import requires_db

pytestmark = requires_db

SCHEDULED = datetime(2026, 9, 6, 5, 15, tzinfo=UTC)


def make_normalized(**overrides) -> NormalizedFlight:
    defaults = {
        "flight_number": "TK878",
        "origin_iata": "IST",
        "destination_iata": "IKA",
        "scheduled_departure_utc": SCHEDULED,
        "flight_iata": "TK878",
        "airline_iata": "TK",
        "airline_name": "Turkish Airlines",
        "status": FlightStatus.SCHEDULED,
        "raw_status": "Expected",
        "delay_minutes": 0,
        "data_source": "test-provider",
        "observed_at": datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return NormalizedFlight(**defaults)


class StubProvider:
    """A provider whose response the test controls cycle by cycle."""

    def __init__(self, batches: list[list[NormalizedFlight]]) -> None:
        self.batches = batches
        self.calls = 0

    async def fetch(self, *_args, **_kwargs):
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        return ProviderResult(provider="test-provider", flights=batch), ["test-provider"], []


@pytest.fixture
def collector(monkeypatch) -> FlightCollector:
    registry = ProviderRegistry(["mock"])
    return FlightCollector(registry)


async def run_cycle(collector: FlightCollector, session, stub: StubProvider) -> None:
    async def fake(*args, **kwargs):
        return await stub.fetch(*args, **kwargs)

    collector.registry.fetch_with_failover = fake  # type: ignore[method-assign]
    await collector.run_once(session, trigger="test")


class TestAppendOnlyHistory:
    async def test_each_cycle_appends_a_new_observation(self, db_session, collector) -> None:
        """The core guarantee: history accumulates, it is never overwritten."""
        stub = StubProvider(
            [
                [make_normalized(delay_minutes=0, status=FlightStatus.SCHEDULED)],
                [make_normalized(delay_minutes=35, status=FlightStatus.DELAYED,
                                 observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC))],
                [make_normalized(delay_minutes=70, status=FlightStatus.DEPARTED,
                                 actual_departure_utc=SCHEDULED + timedelta(minutes=70),
                                 observed_at=datetime(2026, 9, 6, 6, 30, tzinfo=UTC))],
            ]
        )
        for _ in range(3):
            await run_cycle(collector, db_session, stub)

        flights = db_session.scalars(select(Flight)).all()
        assert len(flights) == 1, "the same leg must not create duplicate flight rows"

        observations = db_session.scalars(
            select(FlightObservation).order_by(FlightObservation.observed_at)
        ).all()
        assert len(observations) == 3
        assert [o.delay_minutes for o in observations] == [0, 35, 70]
        assert [o.status for o in observations] == [
            FlightStatus.SCHEDULED,
            FlightStatus.DELAYED,
            FlightStatus.DEPARTED,
        ]
        # The flight row reflects the latest state.
        assert flights[0].delay_minutes == 70
        assert flights[0].status is FlightStatus.DEPARTED
        assert flights[0].observation_count == 3

    async def test_same_run_and_source_does_not_duplicate(self, db_session, collector) -> None:
        stub = StubProvider([[make_normalized(), make_normalized()]])
        await run_cycle(collector, db_session, stub)
        count = db_session.scalar(select(func.count()).select_from(FlightObservation))
        assert count == 1  # deduplicated before insert


class TestEventDerivation:
    async def test_first_sighting_emits_first_seen(self, db_session, collector) -> None:
        await run_cycle(collector, db_session, StubProvider([[make_normalized()]]))
        events = db_session.scalars(select(FlightEvent)).all()
        assert [e.event_type for e in events] == [EventType.FIRST_SEEN]

    async def test_status_change_and_cancellation_emit_events(self, db_session, collector) -> None:
        stub = StubProvider(
            [
                [make_normalized(status=FlightStatus.SCHEDULED)],
                [
                    make_normalized(
                        status=FlightStatus.CANCELLED,
                        is_cancelled=True,
                        delay_minutes=None,
                        cancellation_reason="Operational",
                        observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC),
                    )
                ],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        types = {e.event_type for e in db_session.scalars(select(FlightEvent)).all()}
        assert EventType.STATUS_CHANGED in types
        assert EventType.CANCELLED in types

    async def test_growing_delay_emits_delay_increased(self, db_session, collector) -> None:
        stub = StubProvider(
            [
                [make_normalized(delay_minutes=10)],
                [make_normalized(delay_minutes=65,
                                 observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC))],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        event = db_session.scalar(
            select(FlightEvent).where(FlightEvent.event_type == EventType.DELAY_INCREASED)
        )
        assert event is not None
        assert event.previous_value == "10"
        assert event.new_value == "65"

    async def test_minor_delay_jitter_emits_nothing(self, db_session, collector) -> None:
        stub = StubProvider(
            [
                [make_normalized(delay_minutes=20)],
                [make_normalized(delay_minutes=22,
                                 observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC))],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)
        delay_events = db_session.scalars(
            select(FlightEvent).where(
                FlightEvent.event_type.in_([EventType.DELAY_INCREASED, EventType.DELAY_DECREASED])
            )
        ).all()
        assert delay_events == []

    async def test_gate_change_is_recorded(self, db_session, collector) -> None:
        stub = StubProvider(
            [
                [make_normalized(gate="F7")],
                [make_normalized(gate="F12", observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC))],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)
        event = db_session.scalar(
            select(FlightEvent).where(FlightEvent.event_type == EventType.GATE_CHANGED)
        )
        assert event is not None and event.new_value == "F12"


class TestMissingDataPolicy:
    async def test_disappearing_flight_is_marked_stale_not_cancelled(
        self, db_session, collector
    ) -> None:
        """The single most important safety rule in the system."""
        old_observation = datetime.now(UTC) - timedelta(hours=6)
        stub = StubProvider(
            [
                [make_normalized(observed_at=old_observation,
                                 scheduled_departure_utc=datetime.now(UTC) + timedelta(hours=2))],
                [],  # the flight vanishes from the board
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.data_quality is DataQuality.STALE
        assert flight.is_cancelled is False
        assert flight.status is not FlightStatus.CANCELLED

        event = db_session.scalar(
            select(FlightEvent).where(FlightEvent.event_type == EventType.WENT_STALE)
        )
        assert event is not None
        assert "not a cancellation" in (event.detail or "")

    async def test_a_flight_older_than_the_window_can_still_go_stale(
        self, db_session, collector
    ) -> None:
        """Observed live: flights sat at BOARDING for days.

        The sweep used to only consider flights inside the polled window, so once a
        flight aged past the lookback nothing could ever mark it stale. A provider's
        live board drops a flight when it departs, so the final observation frequently
        catches it mid-transition - and it then looked live indefinitely.
        """
        long_ago = datetime.now(UTC) - timedelta(days=3)
        stub = StubProvider(
            [
                [
                    make_normalized(
                        scheduled_departure_utc=long_ago,
                        observed_at=long_ago,
                        status=FlightStatus.BOARDING,
                    )
                ],
                [],  # the flight is long gone from the board
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.data_quality is DataQuality.STALE
        # Still never inferred as cancelled - the status is left exactly as last seen.
        assert flight.status is FlightStatus.BOARDING
        assert flight.is_cancelled is False

    async def test_a_departed_flight_is_not_marked_stale(
        self, db_session, collector
    ) -> None:
        """Its departure outcome is settled, so dropping off the board is expected.

        DEPARTED is not *terminal* (the flight could still divert), but this system
        measures departure reliability and that question is answered. Flagging it
        stale claims we lost track of something we actually observed.
        """
        long_ago = datetime.now(UTC) - timedelta(days=2)
        stub = StubProvider(
            [
                [
                    make_normalized(
                        scheduled_departure_utc=long_ago,
                        observed_at=long_ago,
                        actual_departure_utc=long_ago,
                        status=FlightStatus.DEPARTED,
                    )
                ],
                [],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.data_quality is not DataQuality.STALE
        assert not any(
            e.event_type is EventType.WENT_STALE for e in flight.events
        )

    async def test_unknown_delay_is_stored_as_null(self, db_session, collector) -> None:
        await run_cycle(
            collector, db_session, StubProvider([[make_normalized(delay_minutes=None)]])
        )
        observation = db_session.scalar(select(FlightObservation))
        assert observation is not None
        assert observation.delay_minutes is None


class TestScheduleRetiming:
    """A re-timed flight must not be able to hide behind a moved schedule."""

    async def test_first_advertised_time_is_captured_once(self, db_session, collector) -> None:
        later = SCHEDULED + timedelta(hours=5, minutes=15)
        stub = StubProvider(
            [
                [make_normalized(scheduled_departure_utc=SCHEDULED)],
                [
                    make_normalized(
                        scheduled_departure_utc=later,
                        observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC),
                    )
                ],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.first_scheduled_departure_utc == SCHEDULED  # never overwritten
        assert flight.scheduled_departure_utc == later
        assert flight.schedule_moved_minutes == 315

    async def test_displacement_exposes_a_retime_that_delay_hides(
        self, db_session, collector
    ) -> None:
        """The whole point: delay_minutes=0 but the passenger waited 5 hours."""
        later = SCHEDULED + timedelta(hours=5, minutes=15)
        stub = StubProvider(
            [
                [make_normalized(scheduled_departure_utc=SCHEDULED)],
                [
                    make_normalized(
                        scheduled_departure_utc=later,
                        actual_departure_utc=later,  # left exactly on the new time
                        status=FlightStatus.DEPARTED,
                        delay_minutes=0,
                        observed_at=datetime(2026, 9, 6, 5, 0, tzinfo=UTC),
                    )
                ],
            ]
        )
        await run_cycle(collector, db_session, stub)
        await run_cycle(collector, db_session, stub)

        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.delay_minutes == 0            # on time against the new schedule
        assert flight.total_displacement_minutes == 315  # but 5h15m later than advertised

    async def test_an_unmoved_schedule_reports_zero_movement(
        self, db_session, collector
    ) -> None:
        await run_cycle(collector, db_session, StubProvider([[make_normalized()]]))
        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.schedule_moved_minutes == 0


class TestRawPayloadRetention:
    """Provider responses cannot be re-fetched, so they are kept verbatim."""

    async def test_raw_payload_is_stored_with_the_observation(
        self, db_session, collector
    ) -> None:
        payload = {
            "number": "TK878",
            "status": "Expected",
            # Fields the schema deliberately does not model - they must survive anyway.
            "departure": {"checkInDesk": "K", "quality": ["Basic", "Live"]},
            "isCargo": False,
        }
        await run_cycle(
            collector, db_session, StubProvider([[make_normalized(raw=payload)]])
        )
        observation = db_session.scalar(select(FlightObservation))
        assert observation is not None
        assert observation.raw_payload == payload
        # The unmodelled fields are recoverable from history.
        assert observation.raw_payload["departure"]["checkInDesk"] == "K"
        assert observation.raw_payload["departure"]["quality"] == ["Basic", "Live"]

    async def test_retention_can_be_disabled(self, db_session, collector, monkeypatch) -> None:
        import app.services.collector as collector_module
        from app.core.config import Settings

        monkeypatch.setattr(
            collector_module, "settings", Settings(store_raw_payload=False)
        )
        await run_cycle(
            collector, db_session, StubProvider([[make_normalized(raw={"a": 1})]])
        )
        observation = db_session.scalar(select(FlightObservation))
        assert observation is not None
        assert observation.raw_payload is None


class TestDiscoveryAndDeduplication:
    async def test_airlines_are_discovered_from_the_feed(self, db_session, collector) -> None:
        stub = StubProvider(
            [
                [
                    make_normalized(flight_number="TK878", airline_iata="TK",
                                    airline_name="Turkish Airlines"),
                    make_normalized(flight_number="W5117", airline_iata="W5",
                                    airline_name="Mahan Air", destination_iata="MHD"),
                ]
            ]
        )
        await run_cycle(collector, db_session, stub)
        airlines = {a.iata: a.name for a in db_session.scalars(select(Airline)).all()}
        assert airlines == {"TK": "Turkish Airlines", "W5": "Mahan Air"}

    def test_deduplication_keeps_the_richer_record(self) -> None:
        sparse = make_normalized()
        rich = make_normalized(
            terminal="I", gate="F7", aircraft_type="B77W",
            actual_departure_utc=SCHEDULED, status=FlightStatus.DEPARTED,
        )
        assert _deduplicate([sparse, rich]) == [rich]
        assert _deduplicate([rich, sparse]) == [rich]

    def test_dedup_key_tolerates_schedule_drift(self) -> None:
        a = make_normalized(scheduled_departure_utc=SCHEDULED)
        b = make_normalized(scheduled_departure_utc=SCHEDULED + timedelta(minutes=5))
        assert a.dedup_key() == b.dedup_key()


class TestLocalProjections:
    async def test_local_hour_and_date_are_stored_in_local_time(
        self, db_session, collector
    ) -> None:
        """23:30 UTC is 02:30 the next day in Istanbul."""
        late = datetime(2026, 9, 6, 23, 30, tzinfo=UTC)
        await run_cycle(
            collector, db_session, StubProvider([[make_normalized(scheduled_departure_utc=late)]])
        )
        flight = db_session.scalar(select(Flight))
        assert flight is not None
        assert flight.scheduled_hour_local == 2
        assert flight.flight_date_local.isoformat() == "2026-09-07"
