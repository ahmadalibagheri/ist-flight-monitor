"""Provider abstraction.

Everything above this module speaks :class:`NormalizedFlight` and nothing else.
Adding a data source means implementing :class:`FlightDataProvider` and
registering it - no other layer changes.

    FlightDataProvider (ABC)
        |-- AeroDataBoxProvider
        |-- AviationStackProvider
        |-- FlightAwareProvider
        |-- MockProvider          (development only, never feeds analytics)
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.enums import DataQuality, FlightStatus, ProviderKind


class ProviderError(RuntimeError):
    """Base class for provider failures."""

    #: Whether retrying the same call could plausibly succeed.
    retryable: bool = True

    def __init__(self, message: str, *, provider: str = "", retryable: bool | None = None):
        super().__init__(message)
        self.provider = provider
        if retryable is not None:
            self.retryable = retryable


class ProviderNotConfigured(ProviderError):
    """Credentials or required settings are missing."""

    retryable = False


class ProviderAuthError(ProviderError):
    """Rejected credentials. Retrying will not help."""

    retryable = False


class ProviderRateLimited(ProviderError):
    """Quota or rate limit hit. Carries a cooldown hint when the API supplies one."""

    retryable = True

    def __init__(self, message: str, *, provider: str = "", retry_after_seconds: float | None = None):
        super().__init__(message, provider=provider)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    """Transport failure or 5xx from upstream."""

    retryable = True


class ProviderResponseError(ProviderError):
    """The response arrived but could not be understood."""

    retryable = False


@dataclass(slots=True)
class NormalizedFlight:
    """Provider-agnostic view of one flight leg at one point in time.

    All datetimes are timezone-aware UTC. A ``None`` timing field means the
    provider did not supply it - it is never filled in with a guess, because a
    guessed timestamp becomes a fabricated delay downstream.
    """

    # ---- identity -------------------------------------------------------
    flight_number: str
    origin_iata: str
    destination_iata: str
    scheduled_departure_utc: datetime

    flight_iata: str | None = None
    flight_icao: str | None = None
    airline_iata: str | None = None
    airline_icao: str | None = None
    airline_name: str | None = None

    # ---- timings --------------------------------------------------------
    estimated_departure_utc: datetime | None = None
    actual_departure_utc: datetime | None = None
    scheduled_arrival_utc: datetime | None = None
    estimated_arrival_utc: datetime | None = None
    actual_arrival_utc: datetime | None = None

    # ---- state ----------------------------------------------------------
    status: FlightStatus = FlightStatus.UNKNOWN
    raw_status: str | None = None
    #: Departure delay in minutes as computed by :mod:`app.providers.normalization`,
    #: or supplied directly by the provider. ``None`` == unknown, never 0.
    delay_minutes: int | None = None
    arrival_delay_minutes: int | None = None
    is_cancelled: bool = False
    cancellation_reason: str | None = None
    is_diverted: bool = False

    # ---- extras ---------------------------------------------------------
    terminal: str | None = None
    gate: str | None = None
    aircraft_type: str | None = None
    aircraft_registration: str | None = None
    is_codeshare: bool = False
    codeshare_of: str | None = None

    # ---- provenance -----------------------------------------------------
    data_source: str = "unknown"
    provider_kind: ProviderKind = ProviderKind.REAL
    data_quality: DataQuality = DataQuality.OK
    observed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def route(self) -> str:
        return f"{self.origin_iata}-{self.destination_iata}"

    @property
    def is_mock(self) -> bool:
        return self.provider_kind is ProviderKind.MOCK

    def dedup_key(self) -> tuple[str, str, str, str]:
        """Identity used to merge reports of the same leg across providers.

        Keyed on the UTC scheduled departure *date* rather than the exact instant
        so that providers disagreeing by a few minutes on the schedule still
        resolve to one flight.
        """
        return (
            self.flight_number.replace(" ", "").upper(),
            self.origin_iata.upper(),
            self.destination_iata.upper(),
            self.scheduled_departure_utc.date().isoformat(),
        )


@dataclass(slots=True)
class ProviderResult:
    """What one provider returned for one collection window."""

    provider: str
    flights: list[NormalizedFlight]
    api_calls: int = 0
    cache_hits: int = 0
    latency_ms: float = 0.0
    partial: bool = False
    notes: list[str] = field(default_factory=list)


class FlightDataProvider(abc.ABC):
    """Interface every flight data source implements."""

    #: Stable identifier written to ``data_source`` columns.
    name: str = "base"
    #: REAL data feeds analytics; MOCK is quarantined.
    kind: ProviderKind = ProviderKind.REAL
    #: Human-readable note surfaced by ``GET /providers``.
    description: str = ""

    @property
    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True when every credential and setting this provider needs is present."""

    @abc.abstractmethod
    async def fetch_departures(
        self,
        origin: str,
        destinations: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> ProviderResult:
        """Return departures from ``origin`` to any of ``destinations``.

        Args:
            origin: Origin airport IATA code.
            destinations: Destination IATA codes to keep; others are discarded by
                the provider so the cost of filtering is not paid downstream.
            window_start: Inclusive UTC lower bound on scheduled departure.
            window_end: Exclusive UTC upper bound on scheduled departure.

        Raises:
            ProviderError: on any failure the caller should count against health.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release transport resources. Safe to call repeatedly.

        Deliberately concrete and empty: providers that hold no transport (the
        mock one, for example) should not be forced to implement a no-op.
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name} configured={self.is_configured}>"
