"""Collection cycle: fetch -> upsert -> append observation -> derive events.

The invariant that makes long-term analytics possible: **observations are never
updated**. Each cycle appends one immutable row per flight per provider. The
``flights`` row carries the latest known state as a convenience, but it is always
reconstructible from the observation history.

Missing-data policy (spec section 21) is implemented in :func:`_mark_stale`:
a flight that disappears from the provider board is marked ``DataQuality.STALE``
and nothing else. Its status is left untouched, because absence from a feed is not
evidence of cancellation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import DataQuality, EventType, FlightStatus
from app.core.logging_config import get_logger
from app.core.timeutil import local_date, local_dow, local_hour, utcnow
from app.models import Airline, CollectionRun, Flight, FlightEvent, FlightObservation
from app.providers.base import NormalizedFlight
from app.providers.merge import merge_all
from app.providers.registry import ProviderRegistry

log = get_logger(__name__)

#: Ignore delay jitter below this many minutes when emitting DELAY_* events, so a
#: provider nudging an estimate by a minute does not spam the event log or alerts.
DELAY_EVENT_THRESHOLD_MINUTES = 5


@dataclass(slots=True)
class CollectionSummary:
    """Outcome of one cycle, mirrored into ``collection_runs``."""

    run_id: int | None = None
    provider: str | None = None
    providers_attempted: list[str] = field(default_factory=list)
    flights_seen: int = 0
    flights_created: int = 0
    flights_updated: int = 0
    observations_written: int = 0
    events_written: int = 0
    cancellations_detected: int = 0
    delays_detected: int = 0
    stale_marked: int = 0
    duplicates_skipped: int = 0
    #: Flights that more than one provider described (merge mode only).
    enriched_flights: int = 0
    merge_conflicts: int = 0
    success: bool = False
    error: str | None = None
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "providers_attempted": self.providers_attempted,
            "flights_seen": self.flights_seen,
            "flights_created": self.flights_created,
            "flights_updated": self.flights_updated,
            "observations_written": self.observations_written,
            "events_written": self.events_written,
            "cancellations_detected": self.cancellations_detected,
            "delays_detected": self.delays_detected,
            "stale_marked": self.stale_marked,
            "duplicates_skipped": self.duplicates_skipped,
            "enriched_flights": self.enriched_flights,
            "merge_conflicts": self.merge_conflicts,
            "success": self.success,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 3),
        }


class FlightCollector:
    """Runs one collection cycle against the configured provider chain."""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.registry = registry or ProviderRegistry()

    async def run_once(
        self, session: Session, *, trigger: str = "scheduler"
    ) -> CollectionSummary:
        started = utcnow()
        summary = CollectionSummary()

        window_start = started - timedelta(hours=settings.collection_lookback_hours)
        window_end = started + timedelta(hours=settings.collection_lookahead_hours)

        run = CollectionRun(started_at=started, trigger=trigger)
        session.add(run)
        session.flush()
        summary.run_id = run.id

        log.info(
            "collection.start",
            run_id=run.id,
            trigger=trigger,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            routes=settings.route_labels(),
        )

        per_provider: list[list[NormalizedFlight]] = []
        provenance: dict[tuple[str, str, str, str], dict[str, str]] = {}
        attempted: list[str] = []
        errors: list[str] = []
        providers_used: list[str] = []
        raw_count = 0
        flights: list[NormalizedFlight] = []

        # One fetch per origin airport, because every provider serves a single
        # airport's departure board per request. Routes are grouped by origin so an
        # origin is only asked for the destinations actually configured from it -
        # filtering against one global destination set would also accept pairs
        # nobody asked to monitor.
        #
        # `fetched_origins` records which origins genuinely returned data. It is what
        # keeps the staleness sweep honest: if the IKA board fails while IST succeeds,
        # marking every IKA flight stale would report a provider outage as though the
        # flights had vanished from the schedule.
        fetched_origins: set[str] = set()

        for origin, destinations in settings.destinations_by_origin.items():
            if settings.provider_strategy == "merge":
                results, origin_attempted, origin_errors = await self.registry.fetch_all(
                    session, origin, destinations, window_start, window_end
                )
                origin_batches = [r.flights for r in results]
                origin_providers = [r.provider for r in results]
                succeeded = bool(results)
            else:
                result, origin_attempted, origin_errors = (
                    await self.registry.fetch_with_failover(
                        session, origin, destinations, window_start, window_end
                    )
                )
                origin_batches = [result.flights] if result else []
                origin_providers = [result.provider] if result else []
                succeeded = result is not None

            for name in origin_attempted:
                if name not in attempted:
                    attempted.append(name)
            errors.extend(f"{origin}: {e}" for e in origin_errors)

            if not succeeded:
                log.warning("collection.origin_failed", origin=origin, errors=origin_errors)
                continue

            fetched_origins.add(origin)
            per_provider.extend(origin_batches)
            raw_count += sum(len(b) for b in origin_batches)
            for name in origin_providers:
                if name not in providers_used:
                    providers_used.append(name)

        if not fetched_origins:
            summary.providers_attempted = attempted
            summary.errors = errors
            summary.success = False
            summary.error = "; ".join(errors) or "no provider returned data"
            self._finalise(session, run, summary, started)
            log.error("collection.failed", run_id=run.id, errors=errors)
            return summary

        summary.provider = "+".join(providers_used)

        if settings.provider_strategy == "merge":
            flights, provenance, conflicts = merge_all(per_provider, self.registry.chain)
            summary.merge_conflicts = len(conflicts)
            summary.enriched_flights = sum(
                1
                for key in provenance
                if sum(1 for batch in per_provider for f in batch if f.dedup_key() == key) > 1
            )
        else:
            # Each origin contributes its own batch, so they still need collapsing
            # even in failover mode.
            flights = _deduplicate([f for batch in per_provider for f in batch])

        # A partial cycle is still worth keeping - the origins that answered are
        # recorded, and the ones that did not are reported rather than inferred.
        missing = [o for o in settings.origins if o not in fetched_origins]
        if missing:
            summary.error = f"no data for origin(s): {', '.join(missing)}"
            log.warning("collection.partial", fetched=sorted(fetched_origins), missing=missing)

        summary.providers_attempted = attempted
        summary.errors = errors
        summary.duplicates_skipped = max(0, raw_count - len(flights))
        summary.flights_seen = len(flights)

        # Each provider's own view is kept as its own observation row, so the
        # per-source history survives the merge and a merge rule can be revisited
        # later against what each provider actually said.
        # One observation per provider per run: a provider that lists the same leg
        # twice in one response contributes its richest record, not two rows.
        # (The unique constraint would reject the second anyway, but autoflush is
        # off, so the duplicate must be collapsed here rather than at flush time.)
        best_per_source: dict[tuple[tuple[str, str, str, str], str], NormalizedFlight] = {}
        for batch in per_provider:
            for record in batch:
                slot = (record.dedup_key(), record.data_source)
                current = best_per_source.get(slot)
                if current is None or _richness(record) > _richness(current):
                    best_per_source[slot] = record

        by_key: dict[tuple[str, str, str, str], list[NormalizedFlight]] = {}
        for (key, _source), record in best_per_source.items():
            by_key.setdefault(key, []).append(record)

        seen_ids: set[int] = set()
        for normalized in flights:
            flight, created = self._upsert_flight(
                session, normalized, provenance.get(normalized.dedup_key())
            )
            seen_ids.add(flight.id)
            if created:
                summary.flights_created += 1
            else:
                summary.flights_updated += 1

            for record in by_key.get(normalized.dedup_key(), [normalized]):
                if self._append_observation(session, flight, record, run.id):
                    summary.observations_written += 1

        # Events are derived after the observation exists so an event always has
        # a snapshot to point at.
        session.flush()
        events = self._derive_events(session, run.id)
        summary.events_written = len(events)
        summary.cancellations_detected = sum(
            1 for e in events if e.event_type is EventType.CANCELLED
        )
        summary.delays_detected = sum(
            1 for e in events if e.event_type is EventType.DELAY_INCREASED
        )

        summary.stale_marked = self._mark_stale(session, seen_ids, fetched_origins)
        summary.success = True
        self._finalise(session, run, summary, started)

        log.info("collection.finished", **summary.as_dict())
        return summary

    # ------------------------------------------------------------ persistence
    def _upsert_flight(
        self,
        session: Session,
        n: NormalizedFlight,
        field_sources: dict[str, str] | None = None,
    ) -> tuple[Flight, bool]:
        """Find or create the flight row and refresh its latest-known state."""
        flight_date = local_date(n.scheduled_departure_utc)
        flight = session.scalar(
            select(Flight).where(
                Flight.flight_number == n.flight_number,
                Flight.origin_iata == n.origin_iata,
                Flight.destination_iata == n.destination_iata,
                Flight.flight_date_local == flight_date,
            )
        )
        created = flight is None

        if flight is None:
            flight = Flight(
                flight_number=n.flight_number,
                origin_iata=n.origin_iata,
                destination_iata=n.destination_iata,
                flight_date_local=flight_date,
                scheduled_departure_utc=n.scheduled_departure_utc,
                scheduled_hour_local=local_hour(n.scheduled_departure_utc),
                scheduled_dow_local=local_dow(n.scheduled_departure_utc),
                data_source=n.data_source,
                is_mock=n.is_mock,
                first_seen_at=n.observed_at or utcnow(),
                observation_count=0,
            )
            session.add(flight)

        # Capture the first advertised time before overwriting the current one, so a
        # retime stays measurable. Only ever set when absent.
        if flight.first_scheduled_departure_utc is None:
            flight.first_scheduled_departure_utc = n.scheduled_departure_utc

        # Schedule can legitimately move; keep the local projections in step.
        flight.scheduled_departure_utc = n.scheduled_departure_utc
        flight.scheduled_hour_local = local_hour(n.scheduled_departure_utc)
        flight.scheduled_dow_local = local_dow(n.scheduled_departure_utc)

        flight.flight_iata = n.flight_iata or flight.flight_iata
        flight.flight_icao = n.flight_icao or flight.flight_icao
        flight.airline_iata = n.airline_iata or flight.airline_iata
        flight.airline_icao = n.airline_icao or flight.airline_icao
        flight.airline_name = n.airline_name or flight.airline_name

        flight.estimated_departure_utc = n.estimated_departure_utc
        flight.actual_departure_utc = n.actual_departure_utc or flight.actual_departure_utc
        flight.scheduled_arrival_utc = n.scheduled_arrival_utc or flight.scheduled_arrival_utc
        flight.estimated_arrival_utc = n.estimated_arrival_utc
        flight.actual_arrival_utc = n.actual_arrival_utc or flight.actual_arrival_utc

        flight.status = n.status
        if n.delay_minutes is not None:
            flight.delay_minutes = n.delay_minutes
        if n.arrival_delay_minutes is not None:
            flight.arrival_delay_minutes = n.arrival_delay_minutes
        flight.is_cancelled = n.is_cancelled
        flight.cancellation_reason = n.cancellation_reason or flight.cancellation_reason
        flight.is_diverted = n.is_diverted

        flight.terminal = n.terminal or flight.terminal
        flight.gate = n.gate or flight.gate
        flight.aircraft_type = n.aircraft_type or flight.aircraft_type
        flight.aircraft_registration = n.aircraft_registration or flight.aircraft_registration
        flight.is_codeshare = n.is_codeshare
        flight.codeshare_of = n.codeshare_of or flight.codeshare_of

        flight.data_source = n.data_source[:120]
        if field_sources:
            flight.field_sources = field_sources
        # Once any REAL provider has seen a flight it is no longer mock-only.
        flight.is_mock = flight.is_mock and n.is_mock if not created else n.is_mock
        flight.data_quality = n.data_quality
        flight.last_seen_at = n.observed_at or utcnow()
        flight.observation_count += 1

        session.flush()
        _upsert_airline(session, n)
        return flight, created

    def _append_observation(
        self,
        session: Session,
        flight: Flight,
        n: NormalizedFlight,
        run_id: int | None,
    ) -> bool:
        """Append an immutable snapshot. Returns False if this run already has one."""
        existing = session.scalar(
            select(FlightObservation.id).where(
                FlightObservation.flight_id == flight.id,
                FlightObservation.collection_run_id == run_id,
                FlightObservation.data_source == n.data_source,
            )
        )
        if existing is not None:
            return False

        session.add(
            FlightObservation(
                flight_id=flight.id,
                collection_run_id=run_id,
                observed_at=n.observed_at or utcnow(),
                status=n.status,
                raw_status=n.raw_status,
                delay_minutes=n.delay_minutes,
                arrival_delay_minutes=n.arrival_delay_minutes,
                is_cancelled=n.is_cancelled,
                cancellation_reason=n.cancellation_reason,
                is_diverted=n.is_diverted,
                scheduled_departure_utc=n.scheduled_departure_utc,
                estimated_departure_utc=n.estimated_departure_utc,
                actual_departure_utc=n.actual_departure_utc,
                scheduled_arrival_utc=n.scheduled_arrival_utc,
                estimated_arrival_utc=n.estimated_arrival_utc,
                actual_arrival_utc=n.actual_arrival_utc,
                terminal=n.terminal,
                gate=n.gate,
                aircraft_type=n.aircraft_type,
                aircraft_registration=n.aircraft_registration,
                data_source=n.data_source,
                data_quality=n.data_quality,
                raw_payload=(n.raw or None) if settings.store_raw_payload else None,
            )
        )
        return True

    # ----------------------------------------------------------------- events
    def _derive_events(self, session: Session, run_id: int | None) -> list[FlightEvent]:
        """Compare each observation from this run with its predecessor."""
        observations = session.scalars(
            select(FlightObservation)
            .where(FlightObservation.collection_run_id == run_id)
            .order_by(FlightObservation.flight_id)
        ).all()

        events: list[FlightEvent] = []
        for obs in observations:
            previous = session.scalar(
                select(FlightObservation)
                .where(
                    FlightObservation.flight_id == obs.flight_id,
                    FlightObservation.id != obs.id,
                    FlightObservation.observed_at <= obs.observed_at,
                )
                .order_by(FlightObservation.observed_at.desc(), FlightObservation.id.desc())
                .limit(1)
            )
            events.extend(_diff_observations(previous, obs))

        for event in events:
            session.add(event)
        session.flush()
        return events

    # ------------------------------------------------------------- staleness
    def _mark_stale(
        self, session: Session, seen_ids: set[int], fetched_origins: set[str]
    ) -> int:
        """Flag flights inside the polled window that the provider stopped reporting.

        This deliberately does **not** touch ``status``. A flight vanishing from a
        departure board is a data-quality signal, not a cancellation.
        """
        cutoff = utcnow() - timedelta(minutes=settings.stale_after_minutes)

        # Deliberately *not* limited to the polled window. A provider's live board
        # drops a flight the moment it departs, so the last observation often catches
        # it mid-transition - still SCHEDULED or BOARDING. Once that flight ages past
        # the lookback it would never be revisited, and a window-scoped sweep could
        # never reach it: it sat at BOARDING indefinitely, looking like a live flight
        # hours after departure.
        #
        # The bound is the stale cutoff itself, which is what "we have stopped hearing
        # about this" actually means. Terminal statuses are excluded because they need
        # no further word.
        candidates = session.scalars(
            select(Flight).where(
                # Only origins whose board we actually read this cycle. A flight
                # whose origin failed to fetch has not gone missing from the
                # schedule; we simply did not look.
                Flight.origin_iata.in_(fetched_origins),
                Flight.last_seen_at < cutoff,
                Flight.data_quality != DataQuality.STALE,
                # DEPARTED is included here even though it is not *terminal*: the
                # departure outcome is settled, so the provider dropping the flight
                # afterwards is expected, not a loss of tracking.
                Flight.status.notin_(
                    [
                        FlightStatus.DEPARTED,
                        FlightStatus.ARRIVED,
                        FlightStatus.CANCELLED,
                        FlightStatus.DIVERTED,
                    ]
                ),
            )
        ).all()

        marked = 0
        for flight in candidates:
            if flight.id in seen_ids:
                continue
            flight.data_quality = DataQuality.STALE
            session.add(
                FlightEvent(
                    flight_id=flight.id,
                    event_type=EventType.WENT_STALE,
                    occurred_at=utcnow(),
                    previous_value=flight.status.value,
                    new_value=flight.status.value,
                    detail=(
                        "Flight absent from provider board since "
                        f"{flight.last_seen_at.isoformat()}. Status left unchanged - "
                        "missing data is not a cancellation."
                    ),
                    data_source=flight.data_source,
                )
            )
            marked += 1

        if marked:
            log.warning("collection.stale_flights", count=marked)
        session.flush()
        return marked

    # ------------------------------------------------------------------ misc
    def _finalise(
        self,
        session: Session,
        run: CollectionRun,
        summary: CollectionSummary,
        started: datetime,
    ) -> None:
        finished = utcnow()
        summary.duration_seconds = (finished - started).total_seconds()

        run.finished_at = finished
        run.duration_seconds = summary.duration_seconds
        run.provider = summary.provider
        run.providers_attempted = ",".join(summary.providers_attempted) or None
        run.flights_seen = summary.flights_seen
        run.flights_created = summary.flights_created
        run.flights_updated = summary.flights_updated
        run.observations_written = summary.observations_written
        run.events_written = summary.events_written
        run.cancellations_detected = summary.cancellations_detected
        run.delays_detected = summary.delays_detected
        run.stale_marked = summary.stale_marked
        run.duplicates_skipped = summary.duplicates_skipped
        run.success = summary.success
        run.error = summary.error
        session.flush()


# --------------------------------------------------------------------- helpers
def _deduplicate(flights: Iterable[NormalizedFlight]) -> list[NormalizedFlight]:
    """Collapse repeated reports of the same leg, keeping the richest one."""
    best: dict[tuple[str, str, str, str], NormalizedFlight] = {}
    for flight in flights:
        key = flight.dedup_key()
        current = best.get(key)
        if current is None or _richness(flight) > _richness(current):
            best[key] = flight
    return list(best.values())


def _richness(flight: NormalizedFlight) -> int:
    """How much a record actually tells us - used to pick between duplicates."""
    score = 0
    for value in (
        flight.actual_departure_utc,
        flight.estimated_departure_utc,
        flight.actual_arrival_utc,
        flight.scheduled_arrival_utc,
        flight.terminal,
        flight.gate,
        flight.aircraft_type,
        flight.airline_iata,
    ):
        if value is not None:
            score += 1
    if flight.status is not FlightStatus.UNKNOWN:
        score += 2
    if flight.data_quality is DataQuality.OK:
        score += 1
    return score


def _diff_observations(
    previous: FlightObservation | None, current: FlightObservation
) -> list[FlightEvent]:
    """Turn the delta between two snapshots into lifecycle events."""
    events: list[FlightEvent] = []

    def emit(
        event_type: EventType,
        old: object = None,
        new: object = None,
        detail: str | None = None,
    ) -> None:
        events.append(
            FlightEvent(
                flight_id=current.flight_id,
                event_type=event_type,
                occurred_at=current.observed_at,
                previous_value=None if old is None else str(old)[:120],
                new_value=None if new is None else str(new)[:120],
                detail=detail,
                data_source=current.data_source,
            )
        )

    if previous is None:
        emit(EventType.FIRST_SEEN, None, current.status.value)
        if current.status is FlightStatus.CANCELLED:
            emit(EventType.CANCELLED, None, current.status.value, current.cancellation_reason)
        return events

    if previous.status is not current.status:
        emit(EventType.STATUS_CHANGED, previous.status.value, current.status.value)
        if current.status is FlightStatus.CANCELLED:
            emit(
                EventType.CANCELLED,
                previous.status.value,
                current.status.value,
                current.cancellation_reason,
            )
        elif current.status is FlightStatus.DIVERTED:
            emit(EventType.DIVERTED, previous.status.value, current.status.value)
        elif current.status is FlightStatus.DEPARTED:
            emit(EventType.DEPARTED, previous.status.value, current.status.value)
        elif current.status is FlightStatus.ARRIVED:
            emit(EventType.ARRIVED, previous.status.value, current.status.value)

    # Delay deltas only count when both sides are actually known.
    if previous.delay_minutes is not None and current.delay_minutes is not None:
        change = current.delay_minutes - previous.delay_minutes
        if change >= DELAY_EVENT_THRESHOLD_MINUTES:
            emit(EventType.DELAY_INCREASED, previous.delay_minutes, current.delay_minutes)
        elif change <= -DELAY_EVENT_THRESHOLD_MINUTES:
            emit(EventType.DELAY_DECREASED, previous.delay_minutes, current.delay_minutes)
    elif previous.delay_minutes is None and current.delay_minutes is not None:
        if current.delay_minutes >= settings.delay_threshold_minutes:
            emit(EventType.DELAY_INCREASED, None, current.delay_minutes)

    if previous.gate != current.gate and current.gate:
        emit(EventType.GATE_CHANGED, previous.gate, current.gate)
    if previous.terminal != current.terminal and current.terminal:
        emit(EventType.TERMINAL_CHANGED, previous.terminal, current.terminal)
    if (
        previous.scheduled_departure_utc
        and current.scheduled_departure_utc
        and previous.scheduled_departure_utc != current.scheduled_departure_utc
    ):
        emit(
            EventType.SCHEDULE_CHANGED,
            previous.scheduled_departure_utc.isoformat(),
            current.scheduled_departure_utc.isoformat(),
        )

    return events


def _upsert_airline(session: Session, n: NormalizedFlight) -> None:
    """Discover airlines from the feed rather than maintaining a static list."""
    if not n.airline_iata and not n.airline_icao:
        return
    airline = None
    if n.airline_iata:
        airline = session.scalar(select(Airline).where(Airline.iata == n.airline_iata))
    if airline is None and n.airline_icao:
        airline = session.scalar(select(Airline).where(Airline.icao == n.airline_icao))
    if airline is None:
        session.add(
            Airline(
                iata=n.airline_iata,
                icao=n.airline_icao,
                name=n.airline_name or n.airline_iata or n.airline_icao or "Unknown",
            )
        )
        session.flush()
        return
    if n.airline_name and airline.name != n.airline_name:
        airline.name = n.airline_name
    if n.airline_icao and not airline.icao:
        airline.icao = n.airline_icao
