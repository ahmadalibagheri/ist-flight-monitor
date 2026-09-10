"""FlightAware AeroAPI v4 provider - authoritative tertiary source.

Endpoints
    ``GET /airports/{id}/flights/scheduled_departures``  - not yet departed
    ``GET /airports/{id}/flights/departures``            - already departed

Both are queried and merged: ``scheduled_departures`` carries the upcoming board
(where cancellations show up), ``departures`` carries the settled outcome. This is
the most reliable of the three sources and the only one with a first-class
``cancelled`` boolean, but it bills per returned result, so it sits last in the
default chain.

Units gotcha handled here: AeroAPI reports ``departure_delay`` and
``arrival_delay`` in **seconds**, not minutes.

Auth: ``x-apikey`` header. Docs: https://www.flightaware.com/aeroapi/portal/documentation
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.enums import ProviderKind
from app.core.logging_config import get_logger
from app.core.timeutil import ensure_utc, parse_iso, utcnow
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

#: (endpoint suffix, response key) pairs merged into one board.
_BOARDS: tuple[tuple[str, str], ...] = (
    ("scheduled_departures", "scheduled_departures"),
    ("departures", "departures"),
)
_MAX_PAGES = 3


class FlightAwareProvider(FlightDataProvider):
    name = "flightaware"
    kind = ProviderKind.REAL
    description = (
        "FlightAware AeroAPI v4. Highest data quality with explicit cancelled/"
        "diverted flags; billed per result, so used last in the failover chain."
    )

    def __init__(self) -> None:
        headers = {"Accept": "application/json; charset=UTF-8"}
        if settings.flightaware_api_key:
            headers["x-apikey"] = settings.flightaware_api_key
        self._http = ProviderHTTPClient(
            provider=self.name,
            base_url=settings.flightaware_base_url,
            headers=headers,
        )

    @property
    def is_configured(self) -> bool:
        return bool(settings.flightaware_api_key)

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
                "FLIGHTAWARE_API_KEY is not set", provider=self.name
            )

        self._http.reset_counters()
        wanted = {d.upper() for d in destinations}
        observed_at = utcnow()
        collected: dict[tuple[str, str, str, str], NormalizedFlight] = {}
        notes: list[str] = []

        for suffix, key in _BOARDS:
            cursor: str | None = None
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "start": _iso_z(window_start),
                    "end": _iso_z(window_end),
                    "max_pages": 1,
                }
                if cursor:
                    params["cursor"] = cursor

                payload = await self._http.get_json(
                    f"/airports/{origin.upper()}/flights/{suffix}", params=params
                )
                if not payload:
                    break

                rows = payload.get(key) or []
                if not isinstance(rows, list):
                    notes.append(f"unexpected payload shape for {key}")
                    break

                for item in rows:
                    flight = self._parse(item, origin, wanted, observed_at)
                    if flight is None:
                        continue
                    existing = collected.get(flight.dedup_key())
                    # The settled `departures` board supersedes the scheduled one.
                    if existing is None or _is_more_settled(flight, existing):
                        collected[flight.dedup_key()] = flight

                cursor = _next_cursor(payload)
                if not cursor:
                    break

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
        destination = item.get("destination") or {}
        dest = _clean(destination.get("code_iata")) or _clean(destination.get("code"))
        if not dest:
            return None
        dest = dest.upper()
        if dest not in wanted:
            return None

        number = normalize_flight_number(item.get("ident_iata") or item.get("ident"))
        if not number:
            return None

        codeshares = item.get("codeshares") or []
        # AeroAPI lists the *operating* flight with its codeshare aliases, so a row
        # is only a codeshare duplicate when it is itself position-only.
        if item.get("position_only") and not settings.include_codeshare:
            return None

        sched_dep = _utc(item.get("scheduled_out")) or _utc(item.get("scheduled_off"))
        if sched_dep is None:
            return None

        est_dep = _utc(item.get("estimated_out")) or _utc(item.get("estimated_off"))
        act_dep = _utc(item.get("actual_out")) or _utc(item.get("actual_off"))
        sched_arr = _utc(item.get("scheduled_in")) or _utc(item.get("scheduled_on"))
        est_arr = _utc(item.get("estimated_in")) or _utc(item.get("estimated_on"))
        act_arr = _utc(item.get("actual_in")) or _utc(item.get("actual_on"))

        cancelled = bool(item.get("cancelled"))
        diverted = bool(item.get("diverted"))

        delay = compute_delay_minutes(
            sched_dep, est_dep, act_dep, provider_delay=_seconds_to_minutes(item.get("departure_delay"))
        )
        arrival_delay = compute_delay_minutes(
            sched_arr, est_arr, act_arr, provider_delay=_seconds_to_minutes(item.get("arrival_delay"))
        )

        raw_status = item.get("status")
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

        return NormalizedFlight(
            flight_number=number,
            origin_iata=origin.upper(),
            destination_iata=dest,
            scheduled_departure_utc=sched_dep,
            flight_iata=normalize_flight_number(item.get("ident_iata")),
            flight_icao=normalize_flight_number(item.get("ident_icao")),
            airline_iata=_clean(item.get("operator_iata")),
            airline_icao=_clean(item.get("operator_icao")) or _clean(item.get("operator")),
            airline_name=_clean(item.get("operator")),
            estimated_departure_utc=est_dep,
            actual_departure_utc=act_dep,
            scheduled_arrival_utc=sched_arr,
            estimated_arrival_utc=est_arr,
            actual_arrival_utc=act_arr,
            status=status,
            raw_status=_clean(raw_status),
            delay_minutes=delay,
            arrival_delay_minutes=arrival_delay,
            is_cancelled=cancelled or status.name == "CANCELLED",
            cancellation_reason=None,
            is_diverted=diverted or status.name == "DIVERTED",
            terminal=_clean(item.get("terminal_origin")),
            gate=_clean(item.get("gate_origin")),
            aircraft_type=_clean(item.get("aircraft_type")),
            aircraft_registration=_clean(item.get("registration")),
            is_codeshare=bool(item.get("position_only")),
            codeshare_of=normalize_flight_number(codeshares[0]) if codeshares else None,
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


def _is_more_settled(candidate: NormalizedFlight, existing: NormalizedFlight) -> bool:
    """Prefer the row that knows more about how the flight actually ended."""
    rank = {"UNKNOWN": 0, "SCHEDULED": 1, "BOARDING": 2, "DELAYED": 3,
            "DEPARTED": 4, "DIVERTED": 5, "CANCELLED": 6, "ARRIVED": 7}
    return rank.get(candidate.status.name, 0) > rank.get(existing.status.name, 0)


def _next_cursor(payload: dict[str, Any]) -> str | None:
    links = payload.get("links") or {}
    nxt = links.get("next")
    if not isinstance(nxt, str) or not nxt:
        return None
    # AeroAPI returns a path with a cursor query parameter; keep only the cursor.
    _, _, query = nxt.partition("?")
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == "cursor" and value:
            return value
    return None


def _seconds_to_minutes(value: Any) -> int | None:
    """AeroAPI delays are in seconds. Convert, preserving sign."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value / 60) if value >= 0 else -int(-value / 60)


def _iso_z(value: datetime) -> str:
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[union-attr]


def _utc(value: Any) -> datetime | None:
    parsed = parse_iso(value if isinstance(value, str) else None)
    if parsed is None or parsed.tzinfo is None:
        return None
    return ensure_utc(parsed)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
