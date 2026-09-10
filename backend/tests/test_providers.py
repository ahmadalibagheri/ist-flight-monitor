"""Provider parsing and failure handling, against recorded response shapes.

The payload fixtures below mirror the documented response schemas of each vendor.
No network call is made: ``respx`` intercepts the transport.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.core.config import Settings, get_settings
from app.core.enums import FlightStatus, ProviderKind
from app.providers.aerodatabox import AeroDataBoxProvider, _chunk_window
from app.providers.aviationstack import AviationStackProvider
from app.providers.base import ProviderAuthError, ProviderNotConfigured, ProviderRateLimited
from app.providers.flightaware import FlightAwareProvider, _seconds_to_minutes
from app.providers.mock import MockProvider

WINDOW_START = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _configured_settings(monkeypatch):
    """Give every provider a key so `is_configured` does not short-circuit."""
    get_settings.cache_clear()
    monkeypatch.setenv("AERODATABOX_API_KEY", "test-adb-key")
    monkeypatch.setenv("AVIATIONSTACK_API_KEY", "test-avs-key")
    monkeypatch.setenv("FLIGHTAWARE_API_KEY", "test-fa-key")
    monkeypatch.setenv("ALLOW_MOCK_PROVIDER", "true")
    # Keep retry backoff near-zero so the retry paths are exercised without the
    # suite actually sleeping through production-sized delays.
    monkeypatch.setenv("COLLECTION_MAX_RETRIES", "2")
    monkeypatch.setenv("COLLECTION_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("COLLECTION_BACKOFF_MAX_SECONDS", "0.02")
    monkeypatch.setenv("PROVIDER_CACHE_TTL_SECONDS", "0")
    import app.core.config as config_module

    fresh = Settings()
    monkeypatch.setattr(config_module, "settings", fresh)
    for module in (
        "app.providers.aerodatabox",
        "app.providers.aviationstack",
        "app.providers.flightaware",
        "app.providers.mock",
        "app.providers.http",
        "app.providers.normalization",
    ):
        monkeypatch.setattr(f"{module}.settings", fresh, raising=False)
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------- AeroDataBox
AERODATABOX_PAYLOAD = {
    "departures": [
        {
            "departure": {
                "airport": {"iata": "IST", "icao": "LTFM", "name": "Istanbul"},
                "scheduledTime": {"utc": "2026-09-06 05:15Z", "local": "2026-09-06 08:15+03:00"},
                "revisedTime": {"utc": "2026-09-06 05:50Z", "local": "2026-09-06 08:50+03:00"},
                "terminal": "I",
                "gate": "F7",
            },
            "arrival": {
                "airport": {"iata": "IKA", "icao": "OIIE", "name": "Tehran"},
                "scheduledTime": {"utc": "2026-09-06 08:45Z", "local": "2026-09-06 12:15+03:30"},
            },
            "number": "TK 878",
            "callSign": "THY878",
            "status": "Expected",
            "codeshareStatus": "IsOperator",
            "isCargo": False,
            "aircraft": {"reg": "TC-JJE", "model": "Boeing 777-300ER"},
            "airline": {"name": "Turkish Airlines", "iata": "TK", "icao": "THY"},
        },
        {
            "departure": {
                "airport": {"iata": "IST"},
                "scheduledTime": {"utc": "2026-09-06 06:00Z"},
            },
            "arrival": {"airport": {"iata": "MHD"}, "scheduledTime": {"utc": "2026-09-06 10:00Z"}},
            "number": "W5 117",
            "status": "Canceled",
            "codeshareStatus": "IsOperator",
            "airline": {"name": "Mahan Air", "iata": "W5"},
        },
        {
            # Different destination - must be filtered out by the provider.
            "departure": {"airport": {"iata": "IST"}, "scheduledTime": {"utc": "2026-09-06 07:00Z"}},
            "arrival": {"airport": {"iata": "LHR"}, "scheduledTime": {"utc": "2026-09-06 10:00Z"}},
            "number": "TK 1979",
            "status": "Expected",
            "airline": {"iata": "TK", "name": "Turkish Airlines"},
        },
        {
            # Cargo - excluded.
            "departure": {"airport": {"iata": "IST"}, "scheduledTime": {"utc": "2026-09-06 07:30Z"}},
            "arrival": {"airport": {"iata": "IKA"}, "scheduledTime": {"utc": "2026-09-06 11:00Z"}},
            "number": "TK 6410",
            "status": "Expected",
            "isCargo": True,
            "airline": {"iata": "TK"},
        },
    ]
}


@respx.mock
async def test_aerodatabox_parses_and_filters() -> None:
    respx.get(url__regex=r".*/flights/airports/iata/IST/.*").mock(
        return_value=httpx.Response(200, json=AERODATABOX_PAYLOAD)
    )
    provider = AeroDataBoxProvider()
    result = await provider.fetch_departures("IST", ["IKA", "MHD"], WINDOW_START, WINDOW_END)
    await provider.aclose()

    by_number = {f.flight_number: f for f in result.flights}
    assert set(by_number) == {"TK878", "W5117"}  # LHR and cargo dropped

    tk = by_number["TK878"]
    assert tk.destination_iata == "IKA"
    assert tk.scheduled_departure_utc == datetime(2026, 9, 6, 5, 15, tzinfo=UTC)
    assert tk.estimated_departure_utc == datetime(2026, 9, 6, 5, 50, tzinfo=UTC)
    assert tk.delay_minutes == 35            # revised - scheduled
    assert tk.status is FlightStatus.DELAYED  # 35 min crosses the threshold
    assert tk.terminal == "I" and tk.gate == "F7"
    assert tk.aircraft_type == "Boeing 777-300ER"
    assert tk.airline_iata == "TK"
    assert tk.is_mock is False

    w5 = by_number["W5117"]
    assert w5.is_cancelled is True
    assert w5.status is FlightStatus.CANCELLED
    assert w5.delay_minutes is None          # a cancelled flight has no delay


@respx.mock
async def test_cancelled_flight_reports_no_delay() -> None:
    """Providers echo revisedTime on cancelled records; that must not become 0.

    Observed live: IRZ8257 came back Cancelled with revisedTime == scheduledTime,
    which computed to a delay of zero and read as "cancelled but punctual".
    """
    respx.get(url__regex=r".*/flights/airports/iata/IST/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "departures": [
                    {
                        "departure": {
                            "airport": {"iata": "IST"},
                            "scheduledTime": {"utc": "2026-09-07 20:55Z"},
                            "revisedTime": {"utc": "2026-09-07 20:55Z"},
                        },
                        "arrival": {
                            "airport": {"iata": "IKA"},
                            "scheduledTime": {"utc": "2026-09-08 00:25Z"},
                        },
                        "number": "IRZ 8257",
                        "status": "Canceled",
                        "airline": {"name": "SAHA AIRLINES"},
                    }
                ]
            },
        )
    )
    provider = AeroDataBoxProvider()
    result = await provider.fetch_departures(
        "IST", ["IKA"], datetime(2026, 9, 7, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 9, 0, 0, tzinfo=UTC),
    )
    await provider.aclose()

    flight = result.flights[0]
    assert flight.is_cancelled is True
    assert flight.delay_minutes is None, "a cancelled flight has no departure delay"


@respx.mock
async def test_aerodatabox_missing_schedule_is_dropped_not_guessed() -> None:
    respx.get(url__regex=r".*/flights/airports/iata/IST/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "departures": [
                    {
                        "departure": {"airport": {"iata": "IST"}},  # no scheduledTime
                        "arrival": {"airport": {"iata": "IKA"}},
                        "number": "TK 999",
                        "status": "Expected",
                    }
                ]
            },
        )
    )
    provider = AeroDataBoxProvider()
    result = await provider.fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)
    await provider.aclose()
    assert result.flights == []


@respx.mock
async def test_aerodatabox_auth_failure_is_not_retried() -> None:
    route = respx.get(url__regex=r".*/flights/airports/.*").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )
    provider = AeroDataBoxProvider()
    with pytest.raises(ProviderAuthError):
        await provider.fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)
    await provider.aclose()
    assert route.call_count == 1  # no wasted retries on a bad key


@respx.mock
async def test_rate_limit_raises_with_cooldown_hint() -> None:
    respx.get(url__regex=r".*/flights/airports/.*").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
    )
    provider = AeroDataBoxProvider()
    with pytest.raises(ProviderRateLimited) as excinfo:
        await provider.fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)
    await provider.aclose()
    assert excinfo.value.retry_after_seconds == 30


def test_window_chunking_respects_the_12_hour_cap() -> None:
    chunks = _chunk_window(WINDOW_START, datetime(2026, 9, 7, 12, 0, tzinfo=UTC), 12)
    assert len(chunks) == 3
    assert all((end - start).total_seconds() <= 12 * 3600 for start, end in chunks)
    assert chunks[0][0] == WINDOW_START
    assert chunks[-1][1] == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


# -------------------------------------------------------------- AviationStack
AVIATIONSTACK_PAYLOAD = {
    "pagination": {"limit": 100, "offset": 0, "count": 2, "total": 2},
    "data": [
        {
            "flight_date": "2026-09-06",
            "flight_status": "active",
            "departure": {
                "airport": "Istanbul Airport",
                "iata": "IST",
                "terminal": "I",
                "gate": "F7",
                "delay": 22,
                "scheduled": "2026-09-06T08:15:00+03:00",
                "estimated": "2026-09-06T08:37:00+03:00",
                "actual": "2026-09-06T08:37:00+03:00",
            },
            "arrival": {"iata": "IKA", "scheduled": "2026-09-06T12:15:00+03:30", "delay": None},
            "airline": {"name": "Turkish Airlines", "iata": "TK", "icao": "THY"},
            "flight": {"number": "878", "iata": "TK878", "icao": "THY878", "codeshared": None},
            "aircraft": {"registration": "TC-JJE", "iata": "B77W"},
        },
        {
            "flight_date": "2026-09-06",
            "flight_status": "cancelled",
            "departure": {"iata": "IST", "scheduled": "2026-09-06T09:00:00+03:00", "delay": None},
            "arrival": {"iata": "IKA", "scheduled": "2026-09-06T13:00:00+03:30"},
            "airline": {"name": "Iran Air", "iata": "IR"},
            "flight": {"number": "721", "iata": "IR721", "codeshared": None},
        },
    ],
}


@respx.mock
async def test_aviationstack_parses_status_and_delay() -> None:
    respx.get(url__regex=r".*/flights.*").mock(
        return_value=httpx.Response(200, json=AVIATIONSTACK_PAYLOAD)
    )
    provider = AviationStackProvider()
    result = await provider.fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)
    await provider.aclose()

    by_number = {f.flight_number: f for f in result.flights}
    assert "TK878" in by_number

    tk = by_number["TK878"]
    assert tk.status is FlightStatus.DEPARTED       # "active" + an actual time
    assert tk.delay_minutes == 22
    assert tk.actual_departure_utc == datetime(2026, 9, 6, 5, 37, tzinfo=UTC)
    assert tk.airline_iata == "TK"

    ir = by_number["IR721"]
    assert ir.status is FlightStatus.CANCELLED
    assert ir.is_cancelled is True


@respx.mock
async def test_aviationstack_reports_api_error_without_crashing() -> None:
    respx.get(url__regex=r".*/flights.*").mock(
        return_value=httpx.Response(
            200, json={"error": {"code": "usage_limit_reached", "message": "quota exceeded"}}
        )
    )
    provider = AviationStackProvider()
    result = await provider.fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)
    await provider.aclose()
    assert result.flights == []
    assert any("quota exceeded" in note for note in result.notes)


# ---------------------------------------------------------------- FlightAware
FLIGHTAWARE_PAYLOAD = {
    "scheduled_departures": [
        {
            "ident": "THY878",
            "ident_iata": "TK878",
            "ident_icao": "THY878",
            "operator": "Turkish Airlines",
            "operator_iata": "TK",
            "operator_icao": "THY",
            "origin": {"code_iata": "IST"},
            "destination": {"code_iata": "IKA", "name": "Imam Khomeini Intl"},
            "scheduled_out": "2026-09-06T05:15:00Z",
            "estimated_out": "2026-09-06T06:20:00Z",
            "actual_out": None,
            "scheduled_in": "2026-09-06T08:45:00Z",
            "cancelled": False,
            "diverted": False,
            "departure_delay": 3900,          # SECONDS, not minutes
            "arrival_delay": None,
            "status": "Scheduled",
            "gate_origin": "F7",
            "terminal_origin": "I",
            "aircraft_type": "B77W",
            "registration": "TC-JJE",
            "codeshares": ["AZ7845"],
        },
        {
            "ident_iata": "W5117",
            "operator_iata": "W5",
            "operator": "Mahan Air",
            "origin": {"code_iata": "IST"},
            "destination": {"code_iata": "MHD"},
            "scheduled_out": "2026-09-06T06:00:00Z",
            "cancelled": True,
            "diverted": False,
            "status": "Scheduled",            # stale text; the flag is authoritative
        },
    ],
    "links": None,
    "num_pages": 1,
}


@respx.mock
async def test_flightaware_converts_delay_seconds_to_minutes() -> None:
    respx.get(url__regex=r".*/airports/IST/flights/scheduled_departures.*").mock(
        return_value=httpx.Response(200, json=FLIGHTAWARE_PAYLOAD)
    )
    respx.get(url__regex=r".*/airports/IST/flights/departures.*").mock(
        return_value=httpx.Response(200, json={"departures": []})
    )
    provider = FlightAwareProvider()
    result = await provider.fetch_departures("IST", ["IKA", "MHD"], WINDOW_START, WINDOW_END)
    await provider.aclose()

    by_number = {f.flight_number: f for f in result.flights}
    tk = by_number["TK878"]
    # estimated - scheduled = 65 min, and the provider's 3900s agrees.
    assert tk.delay_minutes == 65
    assert tk.status is FlightStatus.DELAYED
    assert tk.gate == "F7" and tk.terminal == "I"

    w5 = by_number["W5117"]
    assert w5.is_cancelled is True
    assert w5.status is FlightStatus.CANCELLED


def test_seconds_to_minutes_conversion() -> None:
    assert _seconds_to_minutes(3900) == 65
    assert _seconds_to_minutes(-600) == -10
    assert _seconds_to_minutes(None) is None
    assert _seconds_to_minutes(True) is None  # a bool is not a delay


# ----------------------------------------------------------------------- mock
class TestMockQuarantine:
    async def test_mock_output_is_tagged_and_never_real(self) -> None:
        provider = MockProvider()
        assert provider.kind is ProviderKind.MOCK
        result = await provider.fetch_departures(
            "IST", ["IKA", "MHD"], WINDOW_START, datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
        )
        assert result.flights
        assert all(f.is_mock for f in result.flights)
        assert all(f.data_source == "mock" for f in result.flights)

    async def test_mock_refuses_to_run_in_production(self, monkeypatch) -> None:
        import app.providers.mock as mock_module

        production = Settings(environment="production", allow_mock_provider=True)
        monkeypatch.setattr(mock_module, "settings", production)

        with pytest.raises(ProviderNotConfigured, match="never run in production"):
            await MockProvider().fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)

    async def test_mock_refuses_when_flag_is_off(self, monkeypatch) -> None:
        import app.providers.mock as mock_module

        monkeypatch.setattr(mock_module, "settings", Settings(allow_mock_provider=False))
        with pytest.raises(ProviderNotConfigured, match="disabled"):
            await MockProvider().fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)


class TestUnconfiguredProviders:
    async def test_missing_key_raises_not_configured(self, monkeypatch) -> None:
        import app.providers.aerodatabox as adb

        monkeypatch.setattr(adb, "settings", Settings(aerodatabox_api_key=None))
        provider = AeroDataBoxProvider()
        assert provider.is_configured is False
        with pytest.raises(ProviderNotConfigured, match="AERODATABOX_API_KEY"):
            await provider.fetch_departures("IST", ["IKA"], WINDOW_START, WINDOW_END)
