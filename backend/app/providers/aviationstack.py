"""AviationStack provider - secondary source.

Endpoint
    ``GET /v1/flights?access_key=...&dep_iata=IST&arr_iata=IKA``

Its strength is cost: the API filters by route server-side, so monitoring both
routes costs two calls regardless of how busy the airport is. Its weakness is that
the free tier is capped near 100 requests/month and serves **plaintext HTTP only**
- ``AVIATIONSTACK_BASE_URL`` is therefore configurable, and the health check below
warns rather than silently downgrading TLS.

Statuses observed: ``scheduled``, ``active``, ``landed``, ``cancelled``,
``incident``, ``diverted``.

Docs: https://aviationstack.com/documentation
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

#: AviationStack caps ``limit`` at 100 on paid plans.
_PAGE_SIZE = 100
_MAX_PAGES = 5


class AviationStackProvider(FlightDataProvider):
    name = "aviationstack"
    kind = ProviderKind.REAL
    description = (
        "AviationStack real-time flights, filtered server-side by route. "
        "Cheap in call count; free tier is HTTP-only and ~100 requests/month."
    )

    def __init__(self) -> None:
        self._http = ProviderHTTPClient(
            provider=self.name,
            base_url=settings.aviationstack_base_url,
            headers={"Accept": "application/json"},
        )

    @property
    def is_configured(self) -> bool:
        return bool(settings.aviationstack_api_key)

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
                "AVIATIONSTACK_API_KEY is not set", provider=self.name
            )
        if settings.aviationstack_base_url.startswith("http://"):
            log.warning(
                "provider.insecure_transport",
                provider=self.name,
                reason="free tier does not support TLS; API key travels in cleartext",
            )

        self._http.reset_counters()
        observed_at = utcnow()
        collected: dict[tuple[str, str, str, str], NormalizedFlight] = {}
        notes: list[str] = []

        # One request per route: the API filters by dep/arr itself, which is the
        # whole reason this provider is cheap.
        for dest in destinations:
            offset = 0
            for _ in range(_MAX_PAGES):
                payload = await self._http.get_json(
                    "/flights",
                    params={
                        "access_key": settings.aviationstack_api_key,
                        "dep_iata": origin.upper(),
                        "arr_iata": dest.upper(),
                        "limit": _PAGE_SIZE,
                        "offset": offset,
                    },
                )
                if not payload:
                    break

                error = payload.get("error")
                if error:
                    notes.append(f"{dest}: {error.get('message') or error}")
                    break

                rows = payload.get("data") or []
                if not isinstance(rows, list) or not rows:
                    break

                for item in rows:
                    flight = self._parse(item, origin, dest.upper(), observed_at)
                    if flight is None:
                        continue
                    if not window_start <= flight.scheduled_departure_utc < window_end:
                        continue
                    collected.setdefault(flight.dedup_key(), flight)

                pagination = payload.get("pagination") or {}
                total = pagination.get("total")
                offset += len(rows)
                if not isinstance(total, int) or offset >= total:
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
        dest: str,
        observed_at: datetime,
    ) -> NormalizedFlight | None:
        departure = item.get("departure") or {}
        arrival = item.get("arrival") or {}
        flight_block = item.get("flight") or {}

        number = normalize_flight_number(
            flight_block.get("iata") or flight_block.get("icao")
        )
        if not number:
            return None

        codeshared = flight_block.get("codeshared")
        is_codeshare = bool(codeshared)
        if is_codeshare and not settings.include_codeshare:
            return None

        sched_dep = _utc(departure.get("scheduled"))
        if sched_dep is None:
            return None

        est_dep = _utc(departure.get("estimated"))
        act_dep = _utc(departure.get("actual")) or _utc(departure.get("actual_runway"))
        sched_arr = _utc(arrival.get("scheduled"))
        est_arr = _utc(arrival.get("estimated"))
        act_arr = _utc(arrival.get("actual")) or _utc(arrival.get("actual_runway"))

        raw_status = item.get("flight_status")
        raw_lower = (raw_status or "").strip().lower()

        delay = compute_delay_minutes(
            sched_dep, est_dep, act_dep, provider_delay=_int_or_none(departure.get("delay"))
        )
        arrival_delay = compute_delay_minutes(
            sched_arr, est_arr, act_arr, provider_delay=_int_or_none(arrival.get("delay"))
        )

        status = resolve_status(
            raw_status,
            cancelled_flag=raw_lower == "cancelled",
            diverted_flag=raw_lower == "diverted",
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
            flight_iata=normalize_flight_number(flight_block.get("iata")),
            flight_icao=normalize_flight_number(flight_block.get("icao")),
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
            cancellation_reason=None,
            is_diverted=status.name == "DIVERTED",
            terminal=_clean(departure.get("terminal")),
            gate=_clean(departure.get("gate")),
            aircraft_type=_clean(aircraft.get("iata")) or _clean(aircraft.get("icao")),
            aircraft_registration=_clean(aircraft.get("registration")),
            is_codeshare=is_codeshare,
            codeshare_of=normalize_flight_number(
                (codeshared or {}).get("flight_iata") if isinstance(codeshared, dict) else None
            ),
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


def _utc(value: Any) -> datetime | None:
    parsed = parse_iso(value if isinstance(value, str) else None)
    if parsed is None or parsed.tzinfo is None:
        return None
    return ensure_utc(parsed)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
