"""Multi-provider enrichment.

In ``failover`` mode the first provider that answers wins and the rest are never
called. In ``merge`` mode every configured provider is queried and their views of
the same flight are combined, so a source that knows the gate can fill in for one
that only knows the schedule.

Merge rules, in the order they are applied
------------------------------------------

1. **Trust order is chain order.** ``PROVIDER_CHAIN=aerodatabox,airlabs`` means
   AeroDataBox outranks AirLabs. For most fields the highest-trust provider that
   supplied a non-``None`` value wins.

2. **Observed beats predicted, regardless of trust.** A provider reporting an
   *actual* departure time outranks a higher-trust provider that only has an
   estimate. Ground truth is not a matter of vendor reputation - this is what lets
   a free ADS-B feed correct a paid schedule API.

3. **Status and delay are re-derived, never voted on.** After the timing fields
   are merged, status and delay are recomputed from the merged values via
   :mod:`app.providers.normalization`. Picking a status from one provider and
   times from another is how you end up with a flight marked ``SCHEDULED`` that
   has an actual departure time.

4. **Cancellation requires the most-trusted opinion, and disagreement is
   recorded.** A lower-trust provider claiming a cancellation does not override a
   higher-trust provider that says otherwise, because a false cancellation is far
   more damaging than a late one. The disagreement is surfaced in ``conflicts``
   rather than silently discarded.

Every merged record carries ``field_sources``: which provider supplied each field.
Without that, a merged row is unauditable.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.core.config import settings
from app.core.enums import FlightStatus, ProviderKind
from app.core.logging_config import get_logger
from app.providers.base import NormalizedFlight
from app.providers.normalization import (
    assess_quality,
    clear_delay_if_cancelled,
    compute_delay_minutes,
    resolve_status,
)

log = get_logger(__name__)

#: Fields merged by trust order alone.
_SIMPLE_FIELDS: tuple[str, ...] = (
    "flight_iata",
    "flight_icao",
    "airline_iata",
    "airline_icao",
    "airline_name",
    "scheduled_departure_utc",
    "scheduled_arrival_utc",
    "terminal",
    "gate",
    "aircraft_type",
    "aircraft_registration",
    "cancellation_reason",
    "codeshare_of",
)

#: Fields where a *measured* value outranks trust order (rule 2).
_OBSERVED_FIELDS: tuple[str, ...] = (
    "actual_departure_utc",
    "actual_arrival_utc",
)

#: Predicted values: trust order, but never overriding an observed counterpart.
_PREDICTED_FIELDS: tuple[str, ...] = (
    "estimated_departure_utc",
    "estimated_arrival_utc",
)


def merge_records(
    records: list[NormalizedFlight], trust: list[str]
) -> tuple[NormalizedFlight, dict[str, str], list[str]]:
    """Combine several providers' views of one flight leg.

    Args:
        records: Two or more :class:`NormalizedFlight` objects sharing a dedup key.
        trust: Provider names in descending trust order (the configured chain).

    Returns:
        ``(merged, field_sources, conflicts)``.
    """
    if not records:
        raise ValueError("merge_records() needs at least one record")

    # MOCK records must never enrich real ones; the quarantine is absolute.
    real = [r for r in records if r.provider_kind is ProviderKind.REAL]
    usable = real or records
    if len(usable) == 1:
        only = usable[0]
        return only, dict.fromkeys(_all_fields(), only.data_source), []

    rank = {name: index for index, name in enumerate(trust)}
    ordered = sorted(usable, key=lambda r: rank.get(r.data_source, len(rank)))
    base = ordered[0]

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    conflicts: list[str] = []

    # ---- rule 1: trust order -------------------------------------------
    for field in _SIMPLE_FIELDS:
        for record in ordered:
            value = getattr(record, field)
            if value is not None:
                merged[field] = value
                sources[field] = record.data_source
                break

    # ---- rule 2: observed beats predicted ------------------------------
    for field in _OBSERVED_FIELDS:
        providers_with_value = [r for r in ordered if getattr(r, field) is not None]
        if providers_with_value:
            winner = providers_with_value[0]  # already in trust order
            merged[field] = getattr(winner, field)
            sources[field] = winner.data_source
            if len(providers_with_value) > 1:
                spread = {
                    r.data_source: getattr(r, field).isoformat()
                    for r in providers_with_value
                }
                if len({v[:16] for v in spread.values()}) > 1:
                    conflicts.append(f"{field}: {spread}")

    for field in _PREDICTED_FIELDS:
        for record in ordered:
            value = getattr(record, field)
            if value is not None:
                merged[field] = value
                sources[field] = record.data_source
                break

    # ---- rule 4: cancellation / diversion -------------------------------
    cancel_opinions = {r.data_source: r.is_cancelled for r in ordered}
    divert_opinions = {r.data_source: r.is_diverted for r in ordered}
    is_cancelled = ordered[0].is_cancelled
    is_diverted = ordered[0].is_diverted
    sources["is_cancelled"] = ordered[0].data_source
    sources["is_diverted"] = ordered[0].data_source

    if len(set(cancel_opinions.values())) > 1:
        conflicts.append(f"is_cancelled: {cancel_opinions} -> trusted {ordered[0].data_source}")
    if len(set(divert_opinions.values())) > 1:
        conflicts.append(f"is_diverted: {divert_opinions} -> trusted {ordered[0].data_source}")

    # ---- rule 3: re-derive status and delay from the merged timings ------
    scheduled = merged.get("scheduled_departure_utc") or base.scheduled_departure_utc
    delay = compute_delay_minutes(
        scheduled,
        merged.get("estimated_departure_utc"),
        merged.get("actual_departure_utc"),
        provider_delay=next(
            (r.delay_minutes for r in ordered if r.delay_minutes is not None), None
        ),
    )
    arrival_delay = compute_delay_minutes(
        merged.get("scheduled_arrival_utc"),
        merged.get("estimated_arrival_utc"),
        merged.get("actual_arrival_utc"),
        provider_delay=next(
            (r.arrival_delay_minutes for r in ordered if r.arrival_delay_minutes is not None),
            None,
        ),
    )
    status = resolve_status(
        ordered[0].raw_status,
        cancelled_flag=is_cancelled,
        diverted_flag=is_diverted,
        actual_departure=merged.get("actual_departure_utc"),
        actual_arrival=merged.get("actual_arrival_utc"),
        delay_minutes=delay,
        delay_threshold=settings.delay_threshold_minutes,
    )
    delay = clear_delay_if_cancelled(status, delay)
    arrival_delay = clear_delay_if_cancelled(status, arrival_delay)
    sources["status"] = "derived"
    sources["delay_minutes"] = "derived"

    contributors = "+".join(r.data_source for r in ordered)
    merged_flight = replace(
        base,
        **merged,
        status=status,
        delay_minutes=delay,
        arrival_delay_minutes=arrival_delay,
        is_cancelled=is_cancelled or status is FlightStatus.CANCELLED,
        is_diverted=is_diverted or status is FlightStatus.DIVERTED,
        data_source=contributors,
        data_quality=assess_quality(
            status=status,
            scheduled_departure=scheduled,
            delay_minutes=delay,
            has_arrival_data=merged.get("scheduled_arrival_utc") is not None,
        ),
    )

    if conflicts:
        log.warning(
            "merge.conflict",
            flight=base.flight_number,
            route=base.route,
            providers=contributors,
            conflicts=conflicts,
        )
    return merged_flight, sources, conflicts


def merge_all(
    results: list[list[NormalizedFlight]], trust: list[str]
) -> tuple[list[NormalizedFlight], dict[tuple[str, str, str, str], dict[str, str]], list[str]]:
    """Merge every provider's flight list into one enriched list.

    Returns the merged flights, a per-flight field-source map, and all conflicts.
    """
    grouped: dict[tuple[str, str, str, str], list[NormalizedFlight]] = {}
    for batch in results:
        for flight in batch:
            grouped.setdefault(flight.dedup_key(), []).append(flight)

    merged: list[NormalizedFlight] = []
    provenance: dict[tuple[str, str, str, str], dict[str, str]] = {}
    conflicts: list[str] = []

    for key, records in grouped.items():
        flight, sources, record_conflicts = merge_records(records, trust)
        merged.append(flight)
        provenance[key] = sources
        conflicts.extend(record_conflicts)

    log.info(
        "merge.completed",
        input_records=sum(len(b) for b in results),
        merged_flights=len(merged),
        enriched=sum(1 for records in grouped.values() if len(records) > 1),
        conflicts=len(conflicts),
    )
    return merged, provenance, conflicts


def _all_fields() -> tuple[str, ...]:
    return _SIMPLE_FIELDS + _OBSERVED_FIELDS + _PREDICTED_FIELDS
