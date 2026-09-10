"""Alert construction and duplicate suppression."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.core.enums import AlertType, FlightStatus
from app.models import Flight, SentAlert
from app.notifications.alerts import build_alerts, dispatch_alerts
from app.notifications.telegram import TelegramNotifier
from tests.conftest import requires_db


def make_flight(**overrides) -> Flight:
    defaults = {
        "id": 1,
        "flight_number": "TK878",
        "origin_iata": "IST",
        "destination_iata": "IKA",
        "scheduled_departure_utc": datetime(2026, 9, 6, 5, 15, tzinfo=UTC),
        "flight_date_local": date(2026, 9, 6),
        "scheduled_hour_local": 8,
        "scheduled_dow_local": 7,
        "status": FlightStatus.SCHEDULED,
        "airline_iata": "TK",
        "airline_name": "Turkish Airlines",
        "is_cancelled": False,
        "is_diverted": False,
        "delay_minutes": None,
        "data_source": "test",
        "is_mock": False,
    }
    defaults.update(overrides)
    return Flight(**defaults)


class TestAlertRules:
    def test_cancellation_produces_one_alert(self) -> None:
        alerts = build_alerts([make_flight(is_cancelled=True, status=FlightStatus.CANCELLED)])
        cancellations = [a for a in alerts if a.alert_type is AlertType.CANCELLATION]
        assert len(cancellations) == 1
        assert "CANCELLED" in cancellations[0].text
        assert "TK878" in cancellations[0].text

    def test_healthy_flight_produces_nothing(self) -> None:
        assert build_alerts([make_flight(delay_minutes=5)]) == []

    def test_delay_crosses_each_configured_threshold(self) -> None:
        alerts = build_alerts([make_flight(delay_minutes=130, status=FlightStatus.DELAYED)])
        keys = {a.dedup_key for a in alerts if a.alert_type is AlertType.DELAY_THRESHOLD}
        assert keys == {
            "DELAY:TK878:2026-09-06:60",
            "DELAY:TK878:2026-09-06:120",
        }

    def test_delay_below_threshold_is_silent(self) -> None:
        alerts = build_alerts([make_flight(delay_minutes=45, status=FlightStatus.DELAYED)])
        assert [a for a in alerts if a.alert_type is AlertType.DELAY_THRESHOLD] == []

    def test_unknown_delay_never_alerts(self) -> None:
        """No data is not a 60-minute delay."""
        assert build_alerts([make_flight(delay_minutes=None)]) == []

    def test_cancelled_flight_does_not_also_raise_a_delay_alert(self) -> None:
        alerts = build_alerts(
            [make_flight(is_cancelled=True, status=FlightStatus.CANCELLED, delay_minutes=200)]
        )
        assert all(a.alert_type is not AlertType.DELAY_THRESHOLD for a in alerts)

    def test_route_disruption_fires_above_the_rate(self) -> None:
        flights = [
            make_flight(id=i, flight_number=f"TK{800 + i}", is_cancelled=i < 3,
                        status=FlightStatus.CANCELLED if i < 3 else FlightStatus.SCHEDULED)
            for i in range(10)
        ]
        disruptions = [
            a for a in build_alerts(flights) if a.alert_type is AlertType.ROUTE_DISRUPTION
        ]
        assert len(disruptions) == 1
        assert "30.0%" in disruptions[0].text

    def test_route_disruption_needs_a_minimum_number_of_flights(self) -> None:
        """Two cancellations out of two is not a route-wide disruption signal."""
        flights = [
            make_flight(id=i, flight_number=f"TK{800 + i}", is_cancelled=True,
                        status=FlightStatus.CANCELLED)
            for i in range(2)
        ]
        assert [
            a for a in build_alerts(flights) if a.alert_type is AlertType.ROUTE_DISRUPTION
        ] == []

    def test_dedup_key_encodes_the_fact_not_the_moment(self) -> None:
        """A worsening delay reuses the >60 key so it alerts once, not every cycle."""
        first = build_alerts([make_flight(delay_minutes=75, status=FlightStatus.DELAYED)])
        later = build_alerts([make_flight(delay_minutes=95, status=FlightStatus.DELAYED)])
        assert {a.dedup_key for a in first} == {a.dedup_key for a in later}


@requires_db
class TestDeliveryAndDeduplication:
    @pytest.fixture
    def notifier(self, monkeypatch) -> TelegramNotifier:
        import app.notifications.telegram as telegram_module
        from app.core.config import Settings

        configured = Settings(
            telegram_bot_token="123:ABC", telegram_chat_id="-100999", telegram_enabled=True
        )
        monkeypatch.setattr(telegram_module, "settings", configured)
        return TelegramNotifier(bot_token="123:ABC", chat_id="-100999")

    @respx.mock
    async def test_message_is_sent_once_across_repeated_cycles(
        self, db_session, notifier
    ) -> None:
        route = respx.post(url__regex=r".*/bot.*/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        flights = [make_flight(is_cancelled=True, status=FlightStatus.CANCELLED)]

        first = await dispatch_alerts(db_session, flights, notifier)
        second = await dispatch_alerts(db_session, flights, notifier)
        third = await dispatch_alerts(db_session, flights, notifier)

        assert first["sent"] == 1
        assert second["duplicate"] == 1 and second["sent"] == 0
        assert third["duplicate"] == 1 and third["sent"] == 0
        assert route.call_count == 1, "Telegram must be called exactly once"

        ledger = db_session.scalar(select(func.count()).select_from(SentAlert))
        assert ledger == 1

    @respx.mock
    async def test_failed_delivery_releases_the_key_for_retry(
        self, db_session, notifier
    ) -> None:
        """A transport blip must not permanently silence an alert."""
        respx.post(url__regex=r".*/bot.*/sendMessage").mock(
            return_value=httpx.Response(500, text="upstream boom")
        )
        flights = [make_flight(is_cancelled=True, status=FlightStatus.CANCELLED)]
        result = await dispatch_alerts(db_session, flights, notifier)
        assert result["failed"] == 1

        alert = db_session.scalar(select(SentAlert))
        assert alert is not None
        assert alert.delivered is False
        assert alert.dedup_key.startswith("CANCELLATION:TK878:2026-09-06#failed:")

        respx.post(url__regex=r".*/bot.*/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        retry = await dispatch_alerts(db_session, flights, notifier)
        assert retry["sent"] == 1

    async def test_unconfigured_telegram_skips_silently(self, db_session, monkeypatch) -> None:
        import app.notifications.telegram as telegram_module
        from app.core.config import Settings

        monkeypatch.setattr(telegram_module, "settings", Settings(telegram_bot_token=None))
        plain = TelegramNotifier(bot_token=None, chat_id=None)
        result = await dispatch_alerts(
            db_session, [make_flight(is_cancelled=True, status=FlightStatus.CANCELLED)], plain
        )
        assert result["sent"] == 0
        assert result["skipped"] == 1
        assert db_session.scalar(select(func.count()).select_from(SentAlert)) == 0
