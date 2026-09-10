"""Status normalisation and delay derivation.

Providers disagree on vocabulary ("Departed" / "active" / "EnRoute" / "Airborne"
all mean the same thing) and on how much they tell you. This module is the single
place that reconciles them.

Two rules are non-negotiable and enforced here:

1. **Missing data is never a cancellation.** An unmapped or absent status becomes
   :attr:`FlightStatus.UNKNOWN`, never ``CANCELLED``.
2. **An unknown delay is never zero.** :func:`compute_delay_minutes` returns
   ``None`` when no timing pair supports a real measurement, so downstream
   averages exclude it instead of counting it as on time.
"""

from __future__ import annotations

from datetime import datetime

from app.core.enums import DataQuality, FlightStatus
from app.core.timeutil import minutes_between

#: Canonical lowercase token -> normalised status.
#: Covers AeroDataBox, AviationStack, FlightAware and common IATA/FIDS wording.
_STATUS_MAP: dict[str, FlightStatus] = {
    # --- scheduled / planned -------------------------------------------
    "scheduled": FlightStatus.SCHEDULED,
    "expected": FlightStatus.SCHEDULED,
    "planned": FlightStatus.SCHEDULED,
    "ontime": FlightStatus.SCHEDULED,
    "on time": FlightStatus.SCHEDULED,
    "on-time": FlightStatus.SCHEDULED,
    "filed": FlightStatus.SCHEDULED,
    "checkin": FlightStatus.SCHEDULED,
    "check in": FlightStatus.SCHEDULED,
    "checkinopen": FlightStatus.SCHEDULED,
    "notyetdeparted": FlightStatus.SCHEDULED,
    "unknown/scheduled": FlightStatus.SCHEDULED,
    # --- boarding -------------------------------------------------------
    "boarding": FlightStatus.BOARDING,
    "gateopen": FlightStatus.BOARDING,
    "gate open": FlightStatus.BOARDING,
    "gateclosed": FlightStatus.BOARDING,
    "gate closed": FlightStatus.BOARDING,
    "finalcall": FlightStatus.BOARDING,
    "final call": FlightStatus.BOARDING,
    "lastcall": FlightStatus.BOARDING,
    "readyfordeparture": FlightStatus.BOARDING,
    # --- delayed --------------------------------------------------------
    "delayed": FlightStatus.DELAYED,
    "delay": FlightStatus.DELAYED,
    "late": FlightStatus.DELAYED,
    "retarded": FlightStatus.DELAYED,
    "estimated": FlightStatus.DELAYED,
    "rescheduled": FlightStatus.DELAYED,
    # --- departed / airborne --------------------------------------------
    "departed": FlightStatus.DEPARTED,
    "active": FlightStatus.DEPARTED,
    "enroute": FlightStatus.DEPARTED,
    "en route": FlightStatus.DEPARTED,
    "en-route": FlightStatus.DEPARTED,
    "airborne": FlightStatus.DEPARTED,
    "inflight": FlightStatus.DEPARTED,
    "in flight": FlightStatus.DEPARTED,
    "in air": FlightStatus.DEPARTED,
    "takenoff": FlightStatus.DEPARTED,
    "taxiing": FlightStatus.DEPARTED,
    "approaching": FlightStatus.DEPARTED,
    "descending": FlightStatus.DEPARTED,
    # --- arrived ---------------------------------------------------------
    "arrived": FlightStatus.ARRIVED,
    "landed": FlightStatus.ARRIVED,
    "landed / arrived": FlightStatus.ARRIVED,
    "ontheground": FlightStatus.ARRIVED,
    "on ground": FlightStatus.ARRIVED,
    "gatearrival": FlightStatus.ARRIVED,
    "completed": FlightStatus.ARRIVED,
    "arrival": FlightStatus.ARRIVED,
    # --- cancelled -------------------------------------------------------
    "cancelled": FlightStatus.CANCELLED,
    "canceled": FlightStatus.CANCELLED,
    "cancelled_uncertain": FlightStatus.CANCELLED,
    "canceleduncertain": FlightStatus.CANCELLED,
    "cancelleduncertain": FlightStatus.CANCELLED,
    "notoperational": FlightStatus.CANCELLED,
    "not operational": FlightStatus.CANCELLED,
    # --- diverted --------------------------------------------------------
    "diverted": FlightStatus.DIVERTED,
    "redirected": FlightStatus.DIVERTED,
    "divert": FlightStatus.DIVERTED,
    # --- explicitly unknown ----------------------------------------------
    "unknown": FlightStatus.UNKNOWN,
    "incident": FlightStatus.UNKNOWN,
    "nostatus": FlightStatus.UNKNOWN,
    "": FlightStatus.UNKNOWN,
}

#: Statuses that must never be inferred from a *derived* signal - they require an
#: explicit provider flag. Guards rule 1 above.
_REQUIRES_EXPLICIT_FLAG = frozenset({FlightStatus.CANCELLED, FlightStatus.DIVERTED})


def _canonical(raw: str) -> str:
    return "".join(ch for ch in raw.strip().lower() if ch.isalnum() or ch in " -/_").strip()


def normalize_status(raw_status: str | None) -> FlightStatus:
    """Map a provider status string onto the canonical vocabulary.

    Unrecognised input yields :attr:`FlightStatus.UNKNOWN` - deliberately, so an
    unfamiliar vendor string can never be silently read as a cancellation.
    """
    if raw_status is None:
        return FlightStatus.UNKNOWN

    key = _canonical(raw_status)
    if key in _STATUS_MAP:
        return _STATUS_MAP[key]

    compact = key.replace(" ", "").replace("-", "").replace("_", "").replace("/", "")
    if compact in _STATUS_MAP:
        return _STATUS_MAP[compact]

    # Substring fallback, longest token first so "gate closed" does not match
    # "closed" before "gateclosed". Cancellation tokens are matched first because
    # "cancelled (weather)" must not fall through to UNKNOWN.
    for token in ("cancel", "divert"):
        if token in compact:
            return FlightStatus.CANCELLED if token == "cancel" else FlightStatus.DIVERTED
    for token in sorted(_STATUS_MAP, key=len, reverse=True):
        compact_token = token.replace(" ", "").replace("-", "")
        if compact_token and compact_token in compact:
            mapped = _STATUS_MAP[token]
            if mapped in _REQUIRES_EXPLICIT_FLAG:
                continue
            return mapped
    return FlightStatus.UNKNOWN


def compute_delay_minutes(
    scheduled: datetime | None,
    estimated: datetime | None = None,
    actual: datetime | None = None,
    provider_delay: int | None = None,
) -> int | None:
    """Best available departure delay, in whole minutes.

    Preference order, most authoritative first:

    1. ``actual - scheduled``  - what really happened.
    2. ``estimated - scheduled`` - the airline's current expectation.
    3. ``provider_delay``       - the vendor's own number, when it gave us no times.

    Returns ``None`` when none of those are available. A negative result (early
    departure) is preserved rather than clamped, so the caller can distinguish
    "0 minutes late" from "left 10 minutes early".
    """
    if scheduled is not None:
        if actual is not None:
            return minutes_between(actual, scheduled)
        if estimated is not None:
            return minutes_between(estimated, scheduled)
    if provider_delay is not None:
        return int(provider_delay)
    return None


def resolve_status(
    raw_status: str | None,
    *,
    cancelled_flag: bool | None = None,
    diverted_flag: bool | None = None,
    actual_departure: datetime | None = None,
    actual_arrival: datetime | None = None,
    delay_minutes: int | None = None,
    delay_threshold: int = 15,
) -> FlightStatus:
    """Combine a raw status with structured flags into a final status.

    Explicit boolean flags win over the text, because a provider that sets
    ``cancelled: true`` while still reporting a stale "Scheduled" string is
    telling us something the string is not.
    """
    if cancelled_flag:
        return FlightStatus.CANCELLED
    if diverted_flag:
        return FlightStatus.DIVERTED

    mapped = normalize_status(raw_status)
    if mapped in _REQUIRES_EXPLICIT_FLAG:
        # The text says cancelled/diverted; trust it, the flag was simply absent.
        return mapped

    # Hard evidence of movement outranks a stale text status.
    if actual_arrival is not None:
        return FlightStatus.ARRIVED
    if actual_departure is not None:
        return FlightStatus.DEPARTED

    # Only upgrade to DELAYED on a *measured* delay, never on a guess.
    if (
        mapped in {FlightStatus.SCHEDULED, FlightStatus.UNKNOWN}
        and delay_minutes is not None
        and delay_minutes >= delay_threshold
    ):
        return FlightStatus.DELAYED
    return mapped


def clear_delay_if_cancelled(
    status: FlightStatus, delay_minutes: int | None
) -> int | None:
    """Drop a delay reading on a cancelled or diverted flight.

    A cancelled flight never departed, so it has no departure delay - yet providers
    often keep echoing ``revisedTime`` on the cancelled record. When that equals the
    scheduled time it yields a delay of *zero*, which reads as "cancelled but
    punctual" and, worse, as a measurable on-time flight to anything that forgets to
    exclude cancellations.

    The analytics layer already skips cancelled flights, but the stored value and the
    API response should not assert something untrue in the first place.
    """
    if status in {FlightStatus.CANCELLED, FlightStatus.DIVERTED}:
        return None
    return delay_minutes


def assess_quality(
    *,
    status: FlightStatus,
    scheduled_departure: datetime | None,
    delay_minutes: int | None,
    has_arrival_data: bool = True,
) -> DataQuality:
    """Grade how much the timing data on an observation can be trusted."""
    if scheduled_departure is None:
        return DataQuality.UNRELIABLE
    # A delay beyond a day is almost always a timezone or date-rollover bug
    # upstream; flag it rather than let it skew the averages.
    if delay_minutes is not None and not -180 <= delay_minutes <= 1440:
        return DataQuality.UNRELIABLE
    if status is FlightStatus.UNKNOWN:
        return DataQuality.PARTIAL
    if status.has_departed and delay_minutes is None:
        return DataQuality.PARTIAL
    if not has_arrival_data:
        return DataQuality.PARTIAL
    return DataQuality.OK


def normalize_flight_number(value: str | None) -> str | None:
    """Strip spaces and upper-case a flight designator: ``"TK 878"`` -> ``"TK878"``.

    Also drops a leading zero in the numeric part (``"TK0878"`` -> ``"TK878"``) so
    the same leg reported by two providers resolves to one identity.
    """
    if not value:
        return None
    compact = "".join(value.split()).upper()
    if not compact:
        return None
    prefix = "".join(ch for ch in compact if ch.isalpha())
    digits = "".join(ch for ch in compact if ch.isdigit())
    suffix = compact[len(prefix) + len(digits):] if compact.startswith(prefix) else ""
    if not digits:
        return compact
    return f"{prefix}{digits.lstrip('0') or '0'}{suffix}"
