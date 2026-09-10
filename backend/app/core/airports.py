"""Airport reference data needed before the database is reachable.

The ``airports`` table is the system of record and carries the same values (seeded
by migration ``0001``), but the provider layer is deliberately database-free — it
is pure HTTP plus parsing — and it needs an airport's timezone to build a request.

Why that matters: AeroDataBox's FIDS endpoint takes its window as **the origin
airport's local time**, not UTC and not the monitor's operational timezone. While
IST was the only origin those happened to coincide, so using
``OPERATIONAL_TIMEZONE`` was invisibly correct. With a second origin it is not:
Tehran is UTC+03:30 against Istanbul's UTC+03:00, so an IKA window built in
Istanbul time is shifted half an hour and quietly clips flights at both edges.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings

#: IATA code -> IANA timezone. Extend alongside the ``airports`` seed migration
#: when a new airport is monitored.
AIRPORT_TIMEZONES: dict[str, str] = {
    "IST": "Europe/Istanbul",
    "IKA": "Asia/Tehran",
    "MHD": "Asia/Tehran",
}

#: IATA code -> city, for labelling routes in reports and the dashboard.
AIRPORT_CITIES: dict[str, str] = {
    "IST": "Istanbul",
    "IKA": "Tehran",
    "MHD": "Mashhad",
}


def timezone_for(iata: str) -> ZoneInfo:
    """The airport's own timezone, falling back to the operational one.

    An unknown airport falls back rather than raising: a missing entry should
    degrade to the previous behaviour, not stop collection. The fallback is only
    ever wrong by a fixed offset, whereas refusing to run loses the data entirely.
    """
    name = AIRPORT_TIMEZONES.get(iata.strip().upper())
    if name is None:
        return settings.tz
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - bad tzdata
        return settings.tz


def city_for(iata: str) -> str:
    """A human label for the airport, falling back to its IATA code."""
    code = iata.strip().upper()
    return AIRPORT_CITIES.get(code, code)


def route_label(origin: str, destination: str) -> str:
    """``"Istanbul to Tehran"`` — direction included, because direction matters."""
    return f"{city_for(origin)} to {city_for(destination)}"
