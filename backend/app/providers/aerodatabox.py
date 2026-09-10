"""AeroDataBox provider - the default source.

Endpoint
    ``GET /flights/airports/iata/{code}/{fromLocal}/{toLocal}``

Chosen as the primary source because it is the only affordable API that returns a
whole airport departure board *including cancelled flights*, together with
terminal, gate and aircraft - exactly the fields section 3 of the spec asks for.

Constraints handled here:

* One request covers at most 12 hours, so wide collection windows are chunked.
* ``from``/``to`` path segments are **local** airport time without an offset.
* Times arrive as ``{"utc": "2026-09-06 05:15Z", "local": "2026-09-06 08:15+03:00"}``.
* One call returns the board for *all* destinations, so both monitored routes are
  satisfied by a single request - see :mod:`app.providers.http` for the cache that
  keeps it that way.

Docs: https://doc.aerodatabox.com
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.enums import ProviderKind
from app.core.logging_config import get_logger
from app.core.timeutil import ensure_utc, parse_iso, to_local, utcnow
from app.providers.base import (
    FlightDataProvider,
    NormalizedFlight,
    ProviderNotConfigured,
    ProviderResult,
)
from app.providers.http import ProviderHTTPClient
from app.providers.normalization import (
    assess_quality,
    clear_delay_if_cancelled,
    compute_delay_minutes,
    normalize_flight_number,
    resolve_status,
)

log = get_logger(__name__)


class AeroDataBoxProvider(FlightDataProvider):
    name = "aerodatabox"
    kind = ProviderKind.REAL
    description = (
        "AeroDataBox airport FIDS. Full departure board including cancellations, "
        "terminal, gate and aircraft. 12-hour maximum window per request."
    )

    def __init__(self) -> None:
        headers: dict[str, str] = {"Accept": "application/json"}
        if settings.aerodatabox_api_key:
            headers[settings.aerodatabox_auth_header] = settings.aerodatabox_api_key
        if settings.aerodatabox_host_header:
            headers["X-RapidAPI-Host"] = settings.aerodatabox_host_header
        self._http = ProviderHTTPClient(
            provider=self.name,
            base_url=settings.aerodatabox_base_url,
            headers=headers,
        )

    @property
    def is_configured(self) -> bool:
        return bool(settings.aerodatabox_api_key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_departures(
        self,
        origin: str,
        destinations: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> ProviderResult:
        if not self.is_configured:
            raise ProviderNotConfigured(
                "AERODATABOX_API_KEY is not set", provider=self.name
            )

        self._http.reset_counters()
        wanted = {d.upper() for d in destinations}
        observed_at = utcnow()
        collected: dict[tuple[str, str, str, str], NormalizedFlight] = {}
        notes: list[str] = []

        for chunk_start, chunk_end in _chunk_window(
            window_start, window_end, settings.aerodatabox_window_hours
        ):
            # Path segments are local airport time, minute precision, no offset.
            from_local = _local_path(chunk_start)
            to_local = _local_path(chunk_end)
            path = f"/flights/airports/iata/{origin.upper()}/{from_local}/{to_local}"

            payload = await self._http.get_json(
                path,
                params={
                    "withLeg": "true",
                    "direction": "Departure",
                    "withCancelled": "true",
                    "withCodeshared": "true" if settings.include_codeshare else "false",
                    "withCargo": "false",
                    "withPrivate": "false",
                    "withLocation": "false",
                },
            )
            if not payload:
                notes.append(f"empty board for {from_local}..{to_local}")
                continue

            departures = payload.get("departures") or []
            if not isinstance(departures, list):
                notes.append("unexpected payload shape: 'departures' was not a list")
                continue

            for item in departures:
                flight = self._parse(item, origin, wanted, observed_at)
                if flight is None:
                    continue
                # Later chunks may repeat a flight; keep the first parse.
                collected.setdefault(flight.dedup_key(), flight)

        log.info(
            "provider.fetched",
            provider=self.name,
            flights=len(collected),
            api_calls=self._http.api_calls,
            cache_hits=self._http.cache_hits,
        )
        return ProviderResult(
            provider=self.name,
            flights=list(collected.values()),
            api_calls=self._http.api_calls,
            cache_hits=self._http.cache_hits,
            notes=notes,
        )

    # ------------------------------------------------------------------ parse
    def _parse(
        self,
        item: dict[str, Any],
        origin: str,
        wanted: set[str],
        observed_at: datetime,
    ) -> NormalizedFlight | None:
        departure = item.get("departure") or {}
        arrival = item.get("arrival") or {}

        dest = _airport_iata(arrival)
        if not dest or dest not in wanted:
            return None
        if item.get("isCargo"):
            return None

        codeshare_status = (item.get("codeshareStatus") or "").strip()
        is_codeshare = codeshare_status.lower() == "iscodeshared"
        if is_codeshare and not settings.include_codeshare:
            return None

        number = normalize_flight_number(item.get("number"))
        if not number:
            return None

        sched_dep = _pick_time(departure.get("scheduledTime"))
        if sched_dep is None:
            # Without a scheduled departure there is no delay to measure and no
            # stable identity - drop rather than invent one.
            return None

        est_dep = _pick_time(departure.get("revisedTime"))
        act_dep = _pick_time(departure.get("runwayTime"))
        sched_arr = _pick_time(arrival.get("scheduledTime"))
        est_arr = _pick_time(arrival.get("revisedTime"))
        act_arr = _pick_time(arrival.get("runwayTime"))

        raw_status = item.get("status")
        raw_lower = (raw_status or "").strip().lower()
        cancelled = "cancel" in raw_lower
        diverted = "divert" in raw_lower

        delay = compute_delay_minutes(sched_dep, est_dep, act_dep)
        arrival_delay = compute_delay_minutes(sched_arr, est_arr, act_arr)

        status = resolve_status(
            raw_status,
            cancelled_flag=cancelled,
            diverted_flag=diverted,
            actual_departure=act_dep,
            actual_arrival=act_arr,
            delay_minutes=delay,
            delay_threshold=settings.delay_threshold_minutes,
        )
        delay = clear_delay_if_cancelled(status, delay)
        arrival_delay = clear_delay_if_cancelled(status, arrival_delay)

        airline = item.get("airline") or {}
        aircraft = item.get("aircraft") or {}

        return NormalizedFlight(
            flight_number=number,
            origin_iata=origin.upper(),
            destination_iata=dest,
            scheduled_departure_utc=sched_dep,
            flight_iata=number,
            flight_icao=normalize_flight_number(item.get("callSign")),
            airline_iata=_clean(airline.get("iata")),
            airline_icao=_clean(airline.get("icao")),
            airline_name=_clean(airline.get("name")),
            estimated_departure_utc=est_dep,
            actual_departure_utc=act_dep,
            scheduled_arrival_utc=sched_arr,
            estimated_arrival_utc=est_arr,
            actual_arrival_utc=act_arr,
            status=status,
            raw_status=_clean(raw_status),
            delay_minutes=delay,
            arrival_delay_minutes=arrival_delay,
            is_cancelled=status.name == "CANCELLED",
            # AeroDataBox does not publish a reason; leaving it None is honest.
            cancellation_reason=None,
            is_diverted=status.name == "DIVERTED",
            terminal=_clean(departure.get("terminal")),
            gate=_clean(departure.get("gate")),
            aircraft_type=_clean(aircraft.get("model")),
            aircraft_registration=_clean(aircraft.get("reg")),
            is_codeshare=is_codeshare,
            codeshare_of=None,
            data_source=self.name,
            provider_kind=self.kind,
            data_quality=assess_quality(
                status=status,
                scheduled_departure=sched_dep,
                delay_minutes=delay,
                has_arrival_data=sched_arr is not None,
            ),
            observed_at=observed_at,
            raw=item,
        )


# ---------------------------------------------------------------------- helpers
def _chunk_window(
    start: datetime, end: datetime, max_hours: int
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into segments no longer than ``max_hours``."""
    if end <= start:
        return []
    span = timedelta(hours=max(1, max_hours))
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        chunks.append((cursor, nxt))
        cursor = nxt
    return chunks


def _local_path(value: datetime) -> str:
    """Format a UTC instant as the local ``YYYY-MM-DDTHH:MM`` AeroDataBox expects."""
    local = to_local(value)
    assert local is not None
    return local.strftime("%Y-%m-%dT%H:%M")


def _pick_time(block: dict[str, Any] | None) -> datetime | None:
    """Read an AeroDataBox time block, preferring the unambiguous UTC field."""
    if not block:
        return None
    for key in ("utc", "local"):
        parsed = parse_iso(block.get(key))
        if parsed is not None and parsed.tzinfo is not None:
            return ensure_utc(parsed)
    return None


def _airport_iata(block: dict[str, Any] | None) -> str | None:
    airport = (block or {}).get("airport") or {}
    code = airport.get("iata")
    return code.strip().upper() if isinstance(code, str) and code.strip() else None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
