"""Canonical domain enumerations shared by models, providers and analytics."""

from __future__ import annotations

from enum import StrEnum


class FlightStatus(StrEnum):
    """Normalised flight status.

    Providers each use their own vocabulary; :mod:`app.providers.normalization`
    maps every one of them onto exactly these values.
    """

    SCHEDULED = "SCHEDULED"
    BOARDING = "BOARDING"
    DELAYED = "DELAYED"
    DEPARTED = "DEPARTED"
    ARRIVED = "ARRIVED"
    CANCELLED = "CANCELLED"
    DIVERTED = "DIVERTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        """True once no further status change is expected for the flight."""
        return self in _TERMINAL_STATUSES

    @property
    def has_departed(self) -> bool:
        return self in {FlightStatus.DEPARTED, FlightStatus.ARRIVED, FlightStatus.DIVERTED}


    @property
    def is_departure_settled(self) -> bool:
        """True once the departure outcome is known and cannot change.

        Distinct from :attr:`is_terminal`: a DEPARTED flight is still airborne and
        could yet divert, so it is not terminal - but this system measures *departure*
        reliability, and that question is already answered. Marking such a flight
        stale claims we lost track of something we in fact observed.
        """
        return self in _DEPARTURE_SETTLED


_TERMINAL_STATUSES = frozenset(
    {FlightStatus.ARRIVED, FlightStatus.CANCELLED, FlightStatus.DIVERTED}
)

#: Departure outcome known. Includes DEPARTED, which is not terminal.
_DEPARTURE_SETTLED = frozenset(
    {
        FlightStatus.DEPARTED,
        FlightStatus.ARRIVED,
        FlightStatus.CANCELLED,
        FlightStatus.DIVERTED,
    }
)


class EventType(StrEnum):
    """Derived, append-only lifecycle events used for alerting and auditing."""

    FIRST_SEEN = "FIRST_SEEN"
    STATUS_CHANGED = "STATUS_CHANGED"
    DELAY_INCREASED = "DELAY_INCREASED"
    DELAY_DECREASED = "DELAY_DECREASED"
    CANCELLED = "CANCELLED"
    DIVERTED = "DIVERTED"
    DEPARTED = "DEPARTED"
    ARRIVED = "ARRIVED"
    GATE_CHANGED = "GATE_CHANGED"
    TERMINAL_CHANGED = "TERMINAL_CHANGED"
    SCHEDULE_CHANGED = "SCHEDULE_CHANGED"
    WENT_STALE = "WENT_STALE"


class AlertType(StrEnum):
    """Notification categories. Used as part of the deduplication key."""

    CANCELLATION = "CANCELLATION"
    DELAY_THRESHOLD = "DELAY_THRESHOLD"
    ROUTE_DISRUPTION = "ROUTE_DISRUPTION"
    HOURLY_REPORT = "HOURLY_REPORT"
    DAILY_REPORT = "DAILY_REPORT"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"


class StatScope(StrEnum):
    """Dimension a materialised statistic is grouped by."""

    OVERALL = "OVERALL"
    ROUTE = "ROUTE"
    AIRLINE = "AIRLINE"
    FLIGHT = "FLIGHT"
    PERIOD = "PERIOD"          # time-of-day bucket, e.g. "18:00-22:00"
    DOW = "DOW"                # ISO day of week, "1".."7"
    MONTH = "MONTH"            # local calendar month, "YYYY-MM"
    ROUTE_PERIOD = "ROUTE_PERIOD"  # "IST-IKA|18:00-22:00"
    ROUTE_AIRLINE = "ROUTE_AIRLINE"


class DataQuality(StrEnum):
    """How much trust an observation's timing data deserves."""

    OK = "OK"
    PARTIAL = "PARTIAL"       # some fields missing but usable
    STALE = "STALE"           # flight not seen in recent provider responses
    UNRELIABLE = "UNRELIABLE"  # contradictory or unusable timings


class ProviderKind(StrEnum):
    """Whether a provider produces real data. MOCK never feeds analytics."""

    REAL = "REAL"
    MOCK = "MOCK"


# Local-time buckets used across time-of-day analytics and reports.
# (label, start_hour_inclusive, end_hour_exclusive)
TIME_PERIODS: tuple[tuple[str, int, int], ...] = (
    ("00:00-06:00", 0, 6),
    ("06:00-10:00", 6, 10),
    ("10:00-14:00", 10, 14),
    ("14:00-18:00", 14, 18),
    ("18:00-22:00", 18, 22),
    ("22:00-00:00", 22, 24),
)


def period_for_hour(hour: int) -> str:
    """Return the time-of-day bucket label for a local hour (0-23)."""
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    for label, start, end in TIME_PERIODS:
        if start <= hour < end:
            return label
    raise AssertionError("unreachable: TIME_PERIODS must cover 0-23")
