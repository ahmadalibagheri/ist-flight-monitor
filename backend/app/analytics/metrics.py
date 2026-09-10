"""Pure metric computation.

Everything here operates on plain :class:`FlightFact` values with no database
access, which is what makes the delay and cancellation rules cheap to unit-test
and impossible to accidentally couple to a provider.

Denominator policy - the part that is easy to get quietly wrong
---------------------------------------------------------------
* ``cancellation_rate`` is over **all** flights: a cancelled flight is still a
  flight that was scheduled.
* ``delay_rate`` and ``on_time_rate`` are over flights whose delay is actually
  **measurable** - not cancelled, and with a known delay. A flight the provider
  never gave timings for is counted in ``unknown_flights`` and excluded from
  both, because counting it as on-time would silently flatter the airline.
* Delay averages ignore negative delays' sign only where noted; an early
  departure counts as 0 minutes late for the *average lateness*, but is still
  reported honestly in ``max_delay_minutes``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime

from app.core.config import settings
from app.core.enums import FlightStatus


@dataclass(slots=True, frozen=True)
class FlightFact:
    """One flight's outcome, flattened for statistics."""

    flight_id: int
    flight_number: str
    airline_iata: str | None
    airline_icao: str | None
    airline_name: str | None
    origin_iata: str
    destination_iata: str
    scheduled_departure_utc: datetime
    flight_date_local: date
    hour_local: int
    dow_local: int
    status: FlightStatus
    delay_minutes: int | None
    is_cancelled: bool
    is_diverted: bool

    @property
    def route(self) -> str:
        return f"{self.origin_iata}-{self.destination_iata}"

    @property
    def airline_key(self) -> str | None:
        """Stable grouping key for airline statistics.

        Not every carrier publishes an IATA code - smaller Iranian operators on this
        corridor report ICAO only, or nothing but a name. Keying on ``airline_iata``
        alone silently dropped those flights from every airline statistic, including
        the first cancellation this system observed. Falling back keeps them counted
        under a stable identity.
        """
        return self.airline_iata or self.airline_icao or self.airline_name or None

    @property
    def is_completed(self) -> bool:
        """True once the outcome is settled and safe to count in statistics."""
        return self.is_cancelled or self.is_diverted or self.status.has_departed

    @property
    def has_measurable_delay(self) -> bool:
        return not self.is_cancelled and self.delay_minutes is not None

    @property
    def lateness(self) -> int | None:
        """Minutes late, with early departures clamped to 0."""
        if self.delay_minutes is None:
            return None
        return max(0, self.delay_minutes)


@dataclass(slots=True)
class MetricBlock:
    """The metric set shared by every dimension and both statistics tables."""

    total_flights: int = 0
    completed_flights: int = 0
    cancelled_flights: int = 0
    diverted_flights: int = 0
    delayed_flights: int = 0
    on_time_flights: int = 0
    unknown_flights: int = 0

    delayed_gt_15: int = 0
    delayed_gt_30: int = 0
    delayed_gt_60: int = 0
    delayed_gt_120: int = 0

    avg_delay_minutes: float | None = None
    median_delay_minutes: float | None = None
    p90_delay_minutes: float | None = None
    max_delay_minutes: int | None = None

    cancellation_rate: float | None = None
    delay_rate: float | None = None
    on_time_rate: float | None = None
    severe_delay_rate: float | None = None
    reliability_score: float | None = None

    #: Flights with a measurable delay - the denominator for delay/on-time rates.
    measurable_flights: int = 0
    sample_warning: str | None = None
    bucket_percentages: dict[int, float] = field(default_factory=dict)

    def as_columns(self) -> dict[str, object]:
        """Only the fields that exist as columns on the statistics tables."""
        return {
            "total_flights": self.total_flights,
            "completed_flights": self.completed_flights,
            "cancelled_flights": self.cancelled_flights,
            "diverted_flights": self.diverted_flights,
            "delayed_flights": self.delayed_flights,
            "on_time_flights": self.on_time_flights,
            "unknown_flights": self.unknown_flights,
            "delayed_gt_15": self.delayed_gt_15,
            "delayed_gt_30": self.delayed_gt_30,
            "delayed_gt_60": self.delayed_gt_60,
            "delayed_gt_120": self.delayed_gt_120,
            "avg_delay_minutes": self.avg_delay_minutes,
            "median_delay_minutes": self.median_delay_minutes,
            "p90_delay_minutes": self.p90_delay_minutes,
            "max_delay_minutes": self.max_delay_minutes,
            "cancellation_rate": self.cancellation_rate,
            "delay_rate": self.delay_rate,
            "on_time_rate": self.on_time_rate,
            "severe_delay_rate": self.severe_delay_rate,
            "reliability_score": self.reliability_score,
        }


def compute_metrics(
    facts: list[FlightFact],
    *,
    delay_threshold: int | None = None,
    buckets: list[int] | None = None,
    min_sample_size: int | None = None,
) -> MetricBlock:
    """Aggregate a set of flights into a :class:`MetricBlock`.

    Args:
        facts: The flights in this dimension.
        delay_threshold: Minutes at which a flight counts as delayed.
        buckets: Delay bucket boundaries for the ``delayed_gt_*`` counters.
        min_sample_size: When given and the sample is smaller, ``sample_warning``
            is populated so callers can refuse to *rank* on thin data. The metrics
            themselves are still computed and returned.
    """
    threshold = delay_threshold if delay_threshold is not None else settings.delay_threshold_minutes
    bucket_list = buckets if buckets is not None else settings.delay_buckets_minutes

    block = MetricBlock(total_flights=len(facts))
    if not facts:
        return block

    latenesses: list[int] = []
    raw_delays: list[int] = []

    for fact in facts:
        if fact.is_completed:
            block.completed_flights += 1
        if fact.is_cancelled:
            block.cancelled_flights += 1
            continue  # a cancelled flight has no meaningful delay
        if fact.is_diverted:
            block.diverted_flights += 1

        if fact.delay_minutes is None:
            block.unknown_flights += 1
            continue

        lateness = fact.lateness
        assert lateness is not None
        latenesses.append(lateness)
        raw_delays.append(fact.delay_minutes)
        block.measurable_flights += 1

        if lateness >= threshold:
            block.delayed_flights += 1
        else:
            block.on_time_flights += 1

        for bound in bucket_list:
            if lateness > bound:
                attr = f"delayed_gt_{bound}"
                if hasattr(block, attr):
                    setattr(block, attr, getattr(block, attr) + 1)

    # ---- rates ---------------------------------------------------------
    block.cancellation_rate = block.cancelled_flights / block.total_flights

    if block.measurable_flights:
        block.delay_rate = block.delayed_flights / block.measurable_flights
        block.on_time_rate = block.on_time_flights / block.measurable_flights
        block.severe_delay_rate = block.delayed_gt_60 / block.measurable_flights
        block.bucket_percentages = {
            bound: getattr(block, f"delayed_gt_{bound}", 0) / block.measurable_flights
            for bound in bucket_list
            if hasattr(block, f"delayed_gt_{bound}")
        }

    # ---- delay distribution --------------------------------------------
    if latenesses:
        block.avg_delay_minutes = round(statistics.fmean(latenesses), 2)
        block.median_delay_minutes = round(statistics.median(latenesses), 2)
        block.p90_delay_minutes = round(_percentile(latenesses, 90), 2)
    if raw_delays:
        block.max_delay_minutes = max(raw_delays)

    if min_sample_size is not None and block.total_flights < min_sample_size:
        block.sample_warning = (
            f"only {block.total_flights} flights observed; "
            f"{min_sample_size} required for ranking"
        )

    return block


def _percentile(values: list[int], pct: float) -> float:
    """Linear-interpolated percentile. Defined for a single-element list."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight
