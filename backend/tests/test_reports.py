"""Report generation and rendering."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.enums import FlightStatus
from app.models import Flight
from app.reports.builder import build_historical_report, build_hourly_report
from app.reports.render import (
    escape_markdown_v2,
    render_historical_text,
    render_hourly_telegram,
    render_hourly_text,
)
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def populated(db_session):
    """A day of real (non-mock) flights across both routes."""
    from app.core.timeutil import local_date, local_dow, local_hour, utcnow

    today_utc = utcnow()
    rows: list[Flight] = []

    def add(number, dest, hour, delay, cancelled=False, airline="TK"):
        scheduled = today_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            hours=hour
        )
        rows.append(
            Flight(
                flight_number=number,
                origin_iata="IST",
                destination_iata=dest,
                scheduled_departure_utc=scheduled,
                flight_date_local=local_date(scheduled),
                scheduled_hour_local=local_hour(scheduled),
                scheduled_dow_local=local_dow(scheduled),
                status=(
                    FlightStatus.CANCELLED
                    if cancelled
                    else (FlightStatus.DELAYED if (delay or 0) >= 15 else FlightStatus.DEPARTED)
                ),
                delay_minutes=None if cancelled else delay,
                is_cancelled=cancelled,
                is_diverted=False,
                airline_iata=airline,
                airline_name={"TK": "Turkish Airlines", "W5": "Mahan Air"}[airline],
                data_source="test-provider",
                is_mock=False,
                actual_departure_utc=(
                    None if cancelled or delay is None else scheduled + timedelta(minutes=delay)
                ),
            )
        )

    add("TK872", "IKA", 5, 10)
    add("TK874", "IKA", 11, 75)
    add("TK878", "IKA", 16, None, cancelled=True)
    add("W5113", "IKA", 5, 5, airline="W5")
    add("TK886", "MHD", 4, 20)
    add("W5117", "MHD", 8, None, cancelled=True, airline="W5")

    db_session.add_all(rows)
    db_session.flush()
    return rows


class TestHourlyReport:
    def test_reports_both_routes(self, db_session, populated) -> None:
        report = build_hourly_report(db_session)
        assert [b.route for b in report.routes] == ["IST-IKA", "IST-MHD"]
        assert [b.destination_name for b in report.routes] == ["TEHRAN", "MASHHAD"]

    def test_counts_match_the_data(self, db_session, populated) -> None:
        report = build_hourly_report(db_session)
        tehran = next(b for b in report.routes if b.route == "IST-IKA")
        assert tehran.metrics.total_flights == 4
        assert tehran.metrics.cancelled_flights == 1
        assert tehran.metrics.delayed_flights == 1  # the 75-minute one

    def test_lists_current_issues_by_name(self, db_session, populated) -> None:
        report = build_hourly_report(db_session)
        assert {c.flight_number for c in report.cancelled} == {"TK878", "W5117"}
        assert all(c.airline for c in report.cancelled)

    def test_delayed_list_is_sorted_worst_first(self, db_session, populated) -> None:
        report = build_hourly_report(db_session)
        delays = [d.delay_minutes or 0 for d in report.delayed]
        assert delays == sorted(delays, reverse=True)

    def test_declines_to_rank_on_one_day_of_data(self, db_session, populated) -> None:
        """A single day cannot support a "best time to fly" claim."""
        report = build_hourly_report(db_session)
        for verdict in report.period_verdicts:
            assert verdict.best_period is None
            assert verdict.note is not None

    def test_empty_database_produces_an_honest_report(self, db_session) -> None:
        report = build_hourly_report(db_session)
        assert report.data_note is not None
        assert "No real flight data" in report.data_note
        assert all(b.metrics.total_flights == 0 for b in report.routes)


class TestHourlyRendering:
    def test_layout_matches_the_specification(self, db_session, populated) -> None:
        text = render_hourly_text(build_hourly_report(db_session))
        for expected in (
            "IST FLIGHT MONITOR",
            "Generated:",
            "TEHRAN (IST -> IKA)",
            "MASHHAD (IST -> MHD)",
            "Flights today:",
            "Cancellation rate:",
            "CURRENT ISSUES",
            "Cancelled flights:",
            "Delayed flights:",
            "TIME ANALYSIS",
            "RELIABILITY",
        ):
            assert expected in text, f"missing section: {expected}"

    def test_repeatedly_cancelled_flights_reach_the_report(
        self, db_session, populated
    ) -> None:
        """The most actionable finding must appear in the output people actually read.

        These flights sit below the ranking minimum by design, so without an explicit
        section they exist in the API and nowhere a reader would see them.
        """
        from datetime import timedelta

        from app.core.timeutil import local_date, local_dow, local_hour, utcnow

        # A flight cancelled on three separate days.
        base = utcnow().replace(hour=20, minute=55, second=0, microsecond=0)
        for offset in range(1, 4):
            scheduled = base - timedelta(days=offset)
            db_session.add(
                Flight(
                    flight_number="IRZ8257",
                    origin_iata="IST",
                    destination_iata="IKA",
                    scheduled_departure_utc=scheduled,
                    flight_date_local=local_date(scheduled),
                    scheduled_hour_local=local_hour(scheduled),
                    scheduled_dow_local=local_dow(scheduled),
                    status=FlightStatus.CANCELLED,
                    is_cancelled=True,
                    is_diverted=False,
                    delay_minutes=None,
                    airline_iata=None,
                    airline_name="SAHA AIRLINES",
                    data_source="test-provider",
                    is_mock=False,
                )
            )
        db_session.flush()

        report = build_hourly_report(db_session)
        assert any(
            e["flight_number"] == "IRZ8257" for e in report.repeat_cancellations
        )

        text = render_hourly_text(report)
        assert "REPEATEDLY CANCELLED" in text
        assert "IRZ8257" in text
        assert "3 of 3 days (100%)" in text

    def test_no_repeat_section_when_there_is_nothing_to_report(
        self, db_session, populated
    ) -> None:
        text = render_hourly_text(build_hourly_report(db_session))
        assert "REPEATEDLY CANCELLED" not in text

    def test_missing_metrics_render_as_na_not_zero(self, db_session) -> None:
        """"Unknown" and "zero" must not look the same to a reader."""
        text = render_hourly_text(build_hourly_report(db_session))
        assert "Average delay:     n/a" in text
        assert "Cancellation rate: n/a" in text

    def test_no_issues_renders_none(self, db_session) -> None:
        text = render_hourly_text(build_hourly_report(db_session))
        assert "Cancelled flights:\n  None" in text

    def test_telegram_form_is_wrapped_in_a_code_fence(self, db_session, populated) -> None:
        message = render_hourly_telegram(build_hourly_report(db_session))
        assert message.startswith("*IST Flight Monitor*")
        assert message.count("```") == 2

    def test_markdown_v2_escaping(self) -> None:
        assert escape_markdown_v2("IST-IKA (18:00-22:00)") == (
            "IST\\-IKA \\(18:00\\-22:00\\)"
        )


class TestHistoricalReport:
    def test_summarises_the_window(self, db_session, populated) -> None:
        report = build_historical_report(db_session, days=30)
        assert report.days == 30
        assert report.overall["total_flights"] == 6
        assert {r["route"] for r in report.routes} == {"IST-IKA", "IST-MHD"}

    def test_airlines_below_the_minimum_are_not_ranked(self, db_session, populated) -> None:
        report = build_historical_report(db_session, days=30)
        assert report.airlines
        assert all(a["is_ranked"] is False for a in report.airlines)

    def test_renders_expected_sections(self, db_session, populated) -> None:
        text = render_historical_text(build_historical_report(db_session, days=30))
        assert "LAST 30 DAYS" in text
        assert "IST -> IKA" in text
        assert "AIRLINE RELIABILITY" in text
        assert "Cancellation rate:" in text

    def test_empty_window_says_so(self, db_session) -> None:
        report = build_historical_report(db_session, days=30)
        assert report.data_note is not None
        text = render_historical_text(report)
        assert "No real flight data" in text


class TestMockExclusion:
    def test_mock_flights_never_reach_a_report(self, db_session) -> None:
        """The quarantine rule, verified at the report boundary."""
        from app.core.timeutil import local_date, local_dow, local_hour, utcnow

        scheduled = utcnow()
        db_session.add(
            Flight(
                flight_number="XX999",
                origin_iata="IST",
                destination_iata="IKA",
                scheduled_departure_utc=scheduled,
                flight_date_local=local_date(scheduled),
                scheduled_hour_local=local_hour(scheduled),
                scheduled_dow_local=local_dow(scheduled),
                status=FlightStatus.CANCELLED,
                is_cancelled=True,
                is_diverted=False,
                data_source="mock",
                is_mock=True,
            )
        )
        db_session.flush()

        report = build_hourly_report(db_session)
        assert all(b.metrics.total_flights == 0 for b in report.routes)
        assert report.cancelled == []
        assert report.data_note is not None
