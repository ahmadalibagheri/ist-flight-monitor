"""Timezone helpers.

Rules enforced here, once, so no other module has to think about it:

* Every datetime crossing the persistence boundary is timezone-aware and in UTC.
* Analytics bucketing (hour of day, day of week, "flight day") uses the
  *operational* timezone, Europe/Istanbul by default.
* A naive datetime coming from a provider is an error unless the provider
  explicitly documents the zone; providers must attach one before returning.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Convert an aware datetime to UTC. Naive input is rejected."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime reached ensure_utc(); providers must attach a timezone"
        )
    return value.astimezone(UTC)


def assume_tz(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    """Attach ``tz`` to a naive datetime, then convert to UTC.

    Used for provider payloads that document a local-time field without an
    offset (AeroDataBox's ``local`` timestamps, for example).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


def to_local(value: datetime | None, tz: ZoneInfo | None = None) -> datetime | None:
    """Render a stored UTC datetime in the operational timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz or settings.tz)


def local_date(value: datetime, tz: ZoneInfo | None = None) -> date:
    """The local calendar date a UTC instant falls on."""
    local = to_local(value, tz)
    assert local is not None
    return local.date()


def local_hour(value: datetime, tz: ZoneInfo | None = None) -> int:
    local = to_local(value, tz)
    assert local is not None
    return local.hour


def local_dow(value: datetime, tz: ZoneInfo | None = None) -> int:
    """ISO day of week in local time: Monday=1 .. Sunday=7."""
    local = to_local(value, tz)
    assert local is not None
    return local.isoweekday()


def local_day_bounds(
    day: date, tz: ZoneInfo | None = None
) -> tuple[datetime, datetime]:
    """UTC [start, end) instants spanning a local calendar day.

    Correct across DST transitions because the end is derived from the *next*
    local midnight rather than by adding 24 hours.
    """
    zone = tz or settings.tz
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def minutes_between(later: datetime | None, earlier: datetime | None) -> int | None:
    """Whole minutes from ``earlier`` to ``later``; ``None`` if either is missing.

    Both operands are normalised to UTC first, so mixing an aware local time with
    an aware UTC time is safe. Rounds toward zero.
    """
    if later is None or earlier is None:
        return None
    delta = ensure_utc(later) - ensure_utc(earlier)  # type: ignore[operator]
    return int(delta.total_seconds() // 60) if delta.total_seconds() >= 0 else -int(
        (-delta).total_seconds() // 60
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string, tolerating a trailing ``Z`` and a space separator."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
