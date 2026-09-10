"""API integration tests against a real database via FastAPI's TestClient."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.enums import FlightStatus
from app.models import Flight
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def client(db_session):
    """App wired to the rolled-back test session."""
    from app.core.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seeded(db_session):
    from app.core.timeutil import local_date, local_dow, local_hour, utcnow

    now = utcnow()
    rows = []
    # 6 local days x 4 distinct flight numbers, so every row is a unique identity
    # under uq_flights_identity (flight_number, origin, destination, local date).
    for index in range(24):
        day_offset, slot = divmod(index, 4)
        scheduled = now - timedelta(days=day_offset, hours=slot * 5)
        cancelled = index % 7 == 0
        delay = None if cancelled else (index * 7) % 100
        rows.append(
            Flight(
                flight_number=f"TK{870 + slot}",
                origin_iata="IST",
                destination_iata="IKA" if slot % 2 else "MHD",
                scheduled_departure_utc=scheduled,
                flight_date_local=local_date(scheduled),
                scheduled_hour_local=local_hour(scheduled),
                scheduled_dow_local=local_dow(scheduled),
                status=FlightStatus.CANCELLED if cancelled else FlightStatus.DEPARTED,
                delay_minutes=delay,
                is_cancelled=cancelled,
                is_diverted=False,
                airline_iata="TK" if index % 3 else "W5",
                airline_name="Turkish Airlines" if index % 3 else "Mahan Air",
                data_source="test-provider",
                is_mock=False,
            )
        )
    db_session.add_all(rows)
    db_session.flush()
    return rows


class TestHealth:
    def test_health_is_cheap_and_ok(self, client) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["timezone"] == "Europe/Istanbul"

    def test_ready_fails_without_a_real_provider(self, client) -> None:
        """A green probe must not hide "cannot collect anything"."""
        body = client.get("/ready").json()
        assert body["ready"] is False
        assert body["checks"]["providers"]["ok"] is False
        assert body["checks"]["database"]["ok"] is True

    def test_providers_inventory_never_leaks_keys(self, client) -> None:
        rows = client.get("/providers").json()
        names = {p["name"] for p in rows}
        assert {"aerodatabox", "aviationstack", "flightaware", "mock"} <= names
        serialised = str(rows)
        assert "api_key" not in serialised.lower()
        for provider in rows:
            assert set(provider) >= {"name", "kind", "configured", "in_chain"}

    def test_mock_provider_is_labelled(self, client) -> None:
        mock = next(p for p in client.get("/providers").json() if p["name"] == "mock")
        assert mock["kind"] == "MOCK"
        assert "SYNTHETIC" in mock["description"]


class TestFlights:
    def test_list_is_paginated(self, client, seeded) -> None:
        body = client.get("/api/v1/flights?limit=5").json()
        assert len(body["items"]) == 5
        assert body["total"] == 24
        assert body["has_more"] is True

    def test_filter_by_route(self, client, seeded) -> None:
        body = client.get("/api/v1/flights?route=IST-IKA&limit=100").json()
        assert body["items"]
        assert {f["destination_iata"] for f in body["items"]} == {"IKA"}

    def test_named_windows_are_not_silently_ignored(self, client, seeded) -> None:
        """`/flights` filters on explicit dates only.

        A caller passing `window=today` used to receive *every* flight, because the
        parameter was accepted and dropped. It must not be silently honoured-looking:
        callers resolve named windows to dates themselves (see lib/api.ts).
        """
        everything = client.get("/api/v1/flights?limit=100").json()
        with_window = client.get("/api/v1/flights?limit=100&window=today").json()
        # Same result: the window genuinely has no effect here.
        assert with_window["total"] == everything["total"]

    def test_explicit_date_range_narrows_results(self, client, seeded) -> None:
        from app.analytics.queries import local_today

        today = local_today().isoformat()
        scoped = client.get(
            f"/api/v1/flights?limit=100&start_date={today}&end_date={today}"
        ).json()
        everything = client.get("/api/v1/flights?limit=100").json()
        assert scoped["total"] < everything["total"]
        assert all(f["flight_date_local"] == today for f in scoped["items"])

    def test_filter_by_status(self, client, seeded) -> None:
        body = client.get("/api/v1/flights?status=CANCELLED&limit=100").json()
        assert all(f["is_cancelled"] for f in body["items"])

    def test_local_departure_time_is_included(self, client, seeded) -> None:
        item = client.get("/api/v1/flights?limit=1").json()["items"][0]
        assert item["scheduled_departure_local"]

    def test_mock_flights_hidden_by_default(self, client, db_session, seeded) -> None:
        from app.core.timeutil import local_date, local_dow, local_hour, utcnow

        scheduled = utcnow()
        db_session.add(
            Flight(
                flight_number="XX999", origin_iata="IST", destination_iata="IKA",
                scheduled_departure_utc=scheduled, flight_date_local=local_date(scheduled),
                scheduled_hour_local=local_hour(scheduled), scheduled_dow_local=local_dow(scheduled),
                status=FlightStatus.SCHEDULED, is_cancelled=False, is_diverted=False,
                data_source="mock", is_mock=True,
            )
        )
        db_session.flush()

        default = client.get("/api/v1/flights?limit=100").json()
        assert "XX999" not in {f["flight_number"] for f in default["items"]}

        opted_in = client.get("/api/v1/flights?limit=100&include_mock=true").json()
        assert "XX999" in {f["flight_number"] for f in opted_in["items"]}

    def test_flight_history_by_number(self, client, seeded) -> None:
        body = client.get("/api/v1/flights/TK870").json()
        assert body["flight_number"] == "TK870"
        assert body["total_observed"] >= 1

    def test_unknown_flight_number_is_404(self, client, seeded) -> None:
        assert client.get("/api/v1/flights/ZZ000").status_code == 404

    def test_routes_and_airlines(self, client, seeded) -> None:
        routes = client.get("/api/v1/routes").json()
        assert {r["route"] for r in routes} == {"IST-IKA", "IST-MHD"}
        assert all(r["destination_name"] for r in routes)
        assert client.get("/api/v1/airlines").status_code == 200


class TestStatistics:
    def test_headline_statistics(self, client, seeded) -> None:
        body = client.get("/api/v1/statistics?window=30d").json()
        assert body["total_flights"] == 24
        assert body["cancellation_rate"] is not None
        assert body["reliability"]["score"] == body["reliability_score"]

    def test_hourly_returns_a_point_per_hour(self, client, seeded) -> None:
        body = client.get("/api/v1/statistics/hourly?window=30d&from_cache=false").json()
        assert len(body) == 24
        assert [h["hour_local"] for h in body] == list(range(24))

    def test_time_of_day_covers_each_route(self, client, seeded) -> None:
        body = client.get("/api/v1/statistics/time-of-day?window=30d").json()
        assert {r["route"] for r in body} == {"IST-IKA", "IST-MHD"}
        for entry in body:
            assert "periods" in entry

    def test_airline_ranking_marks_thin_samples(self, client, seeded) -> None:
        body = client.get("/api/v1/statistics/airlines?window=30d").json()
        assert body
        for airline in body:
            assert "is_ranked" in airline
            if not airline["is_ranked"]:
                assert airline["warning"]

    def test_ranked_only_filters(self, client, seeded) -> None:
        assert client.get(
            "/api/v1/statistics/airlines?window=30d&ranked_only=true"
        ).json() == []

    def test_by_scope_supports_month_and_dow(self, client, seeded) -> None:
        for scope in ("MONTH", "DOW", "PERIOD", "AIRLINE", "ROUTE"):
            response = client.get(f"/api/v1/statistics/by/{scope}?window=30d")
            assert response.status_code == 200, scope
            for entry in response.json():
                assert "key" in entry and "is_ranked" in entry

    def test_by_scope_rejects_an_unknown_dimension(self, client) -> None:
        assert client.get("/api/v1/statistics/by/BANANAS?window=30d").status_code == 422

    def test_daily_series_and_trends(self, client, seeded) -> None:
        assert client.get("/api/v1/statistics/daily?window=30d").status_code == 200
        trends = client.get("/api/v1/statistics/trends?days=7").json()
        assert {t["route"] for t in trends} == {"IST-IKA", "IST-MHD"}
        assert all("deltas" in t for t in trends)

    def test_invalid_window_is_rejected(self, client) -> None:
        response = client.get("/api/v1/statistics?window=nonsense")
        assert response.status_code == 400
        assert "Unknown window" in response.json()["detail"]

    def test_inverted_date_range_is_rejected(self, client) -> None:
        response = client.get(
            "/api/v1/statistics?start_date=2026-09-30&end_date=2026-09-01"
        )
        assert response.status_code == 400


class TestReports:
    def test_latest_report_json_and_text(self, client, seeded) -> None:
        body = client.get("/api/v1/reports/latest").json()
        assert len(body["routes"]) == 2
        assert "IST FLIGHT MONITOR" in body["text"]

        text_response = client.get("/api/v1/reports/latest.txt")
        assert text_response.status_code == 200
        assert text_response.headers["content-type"].startswith("text/plain")

    def test_daily_and_monthly_reports(self, client, seeded) -> None:
        assert client.get("/api/v1/reports/daily?days=30").status_code == 200
        monthly = client.get("/api/v1/reports/monthly").json()
        assert monthly["days"] == 30

    def test_config_endpoint_exposes_no_secrets(self, client) -> None:
        body = client.get("/api/v1/reports/config").json()
        assert body["timezone"] == "Europe/Istanbul"
        assert body["destination_airports"] == ["IKA", "MHD"]
        serialised = str(body).lower()
        for forbidden in ("token", "api_key", "password", "secret"):
            assert forbidden not in serialised

    def test_reliability_weights_are_published(self, client) -> None:
        weights = client.get("/api/v1/reports/config").json()["reliability_score"]["weights"]
        assert round(sum(weights.values()), 6) == 1.0


class TestOpenAPI:
    def test_schema_is_served(self, client) -> None:
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"]
        for path in (
            "/health", "/ready", "/providers",
            "/api/v1/flights", "/api/v1/routes", "/api/v1/airlines",
            "/api/v1/statistics", "/api/v1/statistics/delays",
            "/api/v1/statistics/cancellations", "/api/v1/statistics/hourly",
            "/api/v1/statistics/airlines",
            "/api/v1/reports/latest", "/api/v1/reports/daily", "/api/v1/reports/monthly",
        ):
            assert path in schema["paths"], f"undocumented endpoint: {path}"
