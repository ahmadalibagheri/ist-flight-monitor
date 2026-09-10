"""MOCK provider - development and testing ONLY.

.. danger::

   Every flight produced here is **synthetic**. It exists so the dashboard,
   scheduler and report pipeline can be exercised without burning API quota.

   Three independent guards keep it out of production analytics:

   1. It refuses to run unless ``ALLOW_MOCK_PROVIDER=true``.
   2. It refuses to run when ``ENVIRONMENT=production``, regardless of the flag.
   3. Everything it emits is tagged ``ProviderKind.MOCK``, which the collector
      persists as ``flights.is_mock = true``; every analytics query filters those
      rows out. See :mod:`app.analytics.queries`.

Data is generated deterministically from the requested window, so repeated runs
are stable and tests are reproducible.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from app.core.airports import timezone_for
from app.core.config import settings
from app.core.enums import DataQuality, FlightStatus, ProviderKind
from app.core.logging_config import get_logger
from app.core.timeutil import to_local, utcnow
from app.providers.base import (
    FlightDataProvider,
    NormalizedFlight,
    ProviderNotConfigured,
    ProviderResult,
)
from app.providers.normalization import compute_delay_minutes

log = get_logger(__name__)

#: Carriers that genuinely operate these routes, so the synthetic board is
#: structurally realistic. The *statuses and times* below are invented.
#:
#: Keyed by origin as well as destination: keying on destination alone meant a
#: return route asked for destinations {IST} and matched nothing, so the mock
#: provider silently produced zero flights for it.
_SCHEDULE: tuple[tuple[str, str, str, str, str, int, int], ...] = (
    # (flight_number, airline_iata, airline_name, origin, destination, hour, minute)
    ("TK872", "TK", "Turkish Airlines", "IST", "IKA", 5, 30),
    ("TK874", "TK", "Turkish Airlines", "IST", "IKA", 14, 10),
    ("TK878", "TK", "Turkish Airlines", "IST", "IKA", 19, 45),
    ("W5113", "W5", "Mahan Air", "IST", "IKA", 8, 20),
    ("IR721", "IR", "Iran Air", "IST", "IKA", 21, 15),
    ("EP975", "EP", "Iran Aseman Airlines", "IST", "IKA", 23, 5),
    ("TK886", "TK", "Turkish Airlines", "IST", "MHD", 7, 25),
    ("TK888", "TK", "Turkish Airlines", "IST", "MHD", 18, 55),
    ("W5117", "W5", "Mahan Air", "IST", "MHD", 11, 40),
    # Return legs, so a route like IKA-IST is exercised too.
    ("TK873", "TK", "Turkish Airlines", "IKA", "IST", 7, 40),
    ("TK879", "TK", "Turkish Airlines", "IKA", "IST", 22, 30),
    ("W5114", "W5", "Mahan Air", "IKA", "IST", 12, 15),
    ("IR722", "IR", "Iran Air", "IKA", "IST", 16, 50),
    ("TK887", "TK", "Turkish Airlines", "MHD", "IST", 9, 10),
)

#: Nominal block time per airport pair, in minutes. Symmetric, so a return leg
#: does not silently fall through to a default.
_BLOCK_MINUTES: dict[frozenset[str], int] = {
    frozenset({"IST", "IKA"}): 210,
    frozenset({"IST", "MHD"}): 240,
}


class MockProvider(FlightDataProvider):
    name = "mock"
    kind = ProviderKind.MOCK
    description = (
        "SYNTHETIC development data. Never counted in analytics. "
        "Requires ALLOW_MOCK_PROVIDER=true and a non-production environment."
    )

    @property
    def is_configured(self) -> bool:
        return settings.allow_mock_provider and settings.environment != "production"

    async def fetch_departures(
        self,
        origin: str,
        destinations: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> ProviderResult:
        if not settings.allow_mock_provider:
            raise ProviderNotConfigured(
                "MockProvider is disabled. Set ALLOW_MOCK_PROVIDER=true to use it "
                "in development.",
                provider=self.name,
            )
        if settings.environment == "production":
            raise ProviderNotConfigured(
                "MockProvider must never run in production - it emits synthetic data.",
                provider=self.name,
            )

        log.warning(
            "provider.mock_in_use",
            provider=self.name,
            note="SYNTHETIC DATA - excluded from all analytics",
        )

        wanted = {d.upper() for d in destinations}
        origin_code = origin.strip().upper()
        observed_at = utcnow()
        flights: list[NormalizedFlight] = []

        # Departure hours are local to the origin airport, matching how a real
        # timetable is published.
        origin_tz = timezone_for(origin_code)
        start_day = to_local(window_start, origin_tz)
        end_day = to_local(window_end, origin_tz)
        assert start_day is not None and end_day is not None

        day = start_day.date()
        while day <= end_day.date():
            for number, airline_iata, airline_name, sched_origin, dest, hour, minute in _SCHEDULE:
                if sched_origin != origin_code or dest not in wanted:
                    continue
                local_dep = datetime.combine(
                    day, datetime.min.time(), tzinfo=origin_tz
                ).replace(hour=hour, minute=minute)
                sched_dep = local_dep.astimezone(UTC)
                if not window_start <= sched_dep < window_end:
                    continue
                flights.append(
                    self._synthesise(
                        number, airline_iata, airline_name, origin_code, dest,
                        sched_dep, hour, observed_at,
                    )
                )
            day += timedelta(days=1)

        return ProviderResult(
            provider=self.name,
            flights=flights,
            api_calls=0,
            notes=["SYNTHETIC DATA - MockProvider"],
        )

    def _synthesise(
        self,
        number: str,
        airline_iata: str,
        airline_name: str,
        origin: str,
        dest: str,
        sched_dep: datetime,
        local_hour: int,
        observed_at: datetime,
    ) -> NormalizedFlight:
        """Deterministic pseudo-random outcome derived from flight + date."""
        seed = int(
            hashlib.sha256(f"{number}{sched_dep.date()}".encode()).hexdigest()[:8], 16
        )
        # Evening flights are made worse than morning ones so the time-of-day
        # analytics have a visible signal to render during development.
        evening = 18 <= local_hour < 22
        cancel_chance = 12 if evening else 4
        cancelled = seed % 100 < cancel_chance

        block_minutes = _BLOCK_MINUTES.get(frozenset({origin, dest}), 240)
        sched_arr = sched_dep + timedelta(minutes=block_minutes)

        if cancelled:
            return NormalizedFlight(
                flight_number=number,
                origin_iata=origin.upper(),
                destination_iata=dest,
                scheduled_departure_utc=sched_dep,
                flight_iata=number,
                airline_iata=airline_iata,
                airline_name=airline_name,
                scheduled_arrival_utc=sched_arr,
                status=FlightStatus.CANCELLED,
                raw_status="Cancelled",
                is_cancelled=True,
                cancellation_reason="SYNTHETIC - mock provider",
                data_source=self.name,
                provider_kind=self.kind,
                data_quality=DataQuality.OK,
                observed_at=observed_at,
                raw={"mock": True},
            )

        base_delay = (seed // 100) % (75 if evening else 30)
        departed = sched_dep <= observed_at
        actual_dep = sched_dep + timedelta(minutes=base_delay) if departed else None
        est_dep = sched_dep + timedelta(minutes=base_delay) if not departed else None
        delay = compute_delay_minutes(sched_dep, est_dep, actual_dep)

        if departed:
            status = FlightStatus.DEPARTED
        elif delay is not None and delay >= settings.delay_threshold_minutes:
            status = FlightStatus.DELAYED
        else:
            status = FlightStatus.SCHEDULED

        return NormalizedFlight(
            flight_number=number,
            origin_iata=origin.upper(),
            destination_iata=dest,
            scheduled_departure_utc=sched_dep,
            flight_iata=number,
            airline_iata=airline_iata,
            airline_name=airline_name,
            estimated_departure_utc=est_dep,
            actual_departure_utc=actual_dep,
            scheduled_arrival_utc=sched_arr,
            status=status,
            raw_status=status.value.title(),
            delay_minutes=delay,
            terminal="I",
            gate=f"F{(seed % 30) + 1}",
            aircraft_type="Airbus A321" if seed % 2 else "Boeing 737-800",
            data_source=self.name,
            provider_kind=self.kind,
            data_quality=DataQuality.OK,
            observed_at=observed_at,
            raw={"mock": True},
        )
