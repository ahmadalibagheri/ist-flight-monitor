"""Status normalisation and delay derivation.

These tests encode the two safety rules from the specification:
missing data must never become a cancellation, and an unknown delay must never
become zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.enums import FlightStatus
from app.providers.normalization import (
    assess_quality,
    compute_delay_minutes,
    normalize_flight_number,
    normalize_status,
    resolve_status,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # AeroDataBox vocabulary
        ("Expected", FlightStatus.SCHEDULED),
        ("CheckIn", FlightStatus.SCHEDULED),
        ("Boarding", FlightStatus.BOARDING),
        ("GateClosed", FlightStatus.BOARDING),
        ("Departed", FlightStatus.DEPARTED),
        ("EnRoute", FlightStatus.DEPARTED),
        ("Approaching", FlightStatus.DEPARTED),
        ("Arrived", FlightStatus.ARRIVED),
        ("Canceled", FlightStatus.CANCELLED),
        ("CanceledUncertain", FlightStatus.CANCELLED),
        ("Diverted", FlightStatus.DIVERTED),
        # AviationStack vocabulary
        ("scheduled", FlightStatus.SCHEDULED),
        ("active", FlightStatus.DEPARTED),
        ("landed", FlightStatus.ARRIVED),
        ("cancelled", FlightStatus.CANCELLED),
        # FlightAware free text
        ("En Route / On Time", FlightStatus.DEPARTED),
        ("Arrived / Gate Arrival", FlightStatus.ARRIVED),
        ("Cancelled", FlightStatus.CANCELLED),
        # Messy input
        ("  DEPARTED  ", FlightStatus.DEPARTED),
        ("Cancelled (weather)", FlightStatus.CANCELLED),
    ],
)
def test_known_statuses_map_to_canonical_values(raw: str, expected: FlightStatus) -> None:
    assert normalize_status(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "wibble", "???", "Unknown", "NoStatus"])
def test_unrecognised_status_is_unknown_never_cancelled(raw: str | None) -> None:
    """An unfamiliar vendor string must not be read as a cancellation."""
    result = normalize_status(raw)
    assert result is FlightStatus.UNKNOWN
    assert result is not FlightStatus.CANCELLED


def test_missing_data_never_becomes_cancelled() -> None:
    assert resolve_status(None) is FlightStatus.UNKNOWN
    assert resolve_status(None, cancelled_flag=False) is FlightStatus.UNKNOWN
    assert resolve_status("", cancelled_flag=None) is FlightStatus.UNKNOWN


def test_explicit_flag_overrides_stale_text_status() -> None:
    """A provider setting cancelled=true while still saying "Scheduled" is cancelled."""
    assert resolve_status("Scheduled", cancelled_flag=True) is FlightStatus.CANCELLED
    assert resolve_status("Scheduled", diverted_flag=True) is FlightStatus.DIVERTED


def test_actual_times_outrank_stale_text() -> None:
    departed = datetime(2026, 9, 6, 8, 30, tzinfo=UTC)
    assert resolve_status("Scheduled", actual_departure=departed) is FlightStatus.DEPARTED
    assert (
        resolve_status("Scheduled", actual_arrival=departed) is FlightStatus.ARRIVED
    )


def test_delayed_only_inferred_from_measured_delay() -> None:
    assert resolve_status("Scheduled", delay_minutes=40, delay_threshold=15) is FlightStatus.DELAYED
    assert resolve_status("Scheduled", delay_minutes=5, delay_threshold=15) is FlightStatus.SCHEDULED
    # No delay information at all -> no assumption of delay.
    assert resolve_status("Scheduled", delay_minutes=None) is FlightStatus.SCHEDULED


class TestDelayComputation:
    scheduled = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)

    def test_actual_wins_over_estimated(self) -> None:
        actual = datetime(2026, 9, 6, 9, 10, tzinfo=UTC)
        estimated = datetime(2026, 9, 6, 8, 30, tzinfo=UTC)
        assert compute_delay_minutes(self.scheduled, estimated, actual) == 70

    def test_estimated_used_when_no_actual(self) -> None:
        estimated = datetime(2026, 9, 6, 8, 35, tzinfo=UTC)
        assert compute_delay_minutes(self.scheduled, estimated, None) == 35

    def test_provider_delay_is_last_resort(self) -> None:
        assert compute_delay_minutes(None, None, None, provider_delay=42) == 42

    def test_unknown_delay_is_none_not_zero(self) -> None:
        """The critical case: absence of data must not read as an on-time flight."""
        assert compute_delay_minutes(self.scheduled, None, None) is None
        assert compute_delay_minutes(None, None, None) is None

    def test_early_departure_is_negative_not_clamped(self) -> None:
        early = datetime(2026, 9, 6, 7, 50, tzinfo=UTC)
        assert compute_delay_minutes(self.scheduled, None, early) == -10

    def test_mixed_offsets_are_normalised_before_subtracting(self) -> None:
        from zoneinfo import ZoneInfo

        # 11:10 Istanbul == 08:10 UTC, so this is a 10-minute delay, not 190.
        actual_local = datetime(2026, 9, 6, 11, 10, tzinfo=ZoneInfo("Europe/Istanbul"))
        assert compute_delay_minutes(self.scheduled, None, actual_local) == 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TK 878", "TK878"), ("TK0878", "TK878"), ("w5 113", "W5113"), (None, None), ("", None)],
)
def test_flight_number_normalisation(raw: str | None, expected: str | None) -> None:
    assert normalize_flight_number(raw) == expected


def test_quality_flags_impossible_delays() -> None:
    from app.core.enums import DataQuality

    scheduled = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    # A 40-hour "delay" is a date-rollover bug upstream, not a real delay.
    assert (
        assess_quality(
            status=FlightStatus.DEPARTED, scheduled_departure=scheduled, delay_minutes=2400
        )
        is DataQuality.UNRELIABLE
    )
    assert (
        assess_quality(
            status=FlightStatus.DEPARTED, scheduled_departure=None, delay_minutes=10
        )
        is DataQuality.UNRELIABLE
    )


class TestCancelledFlightsHaveNoDelay:
    """A cancelled flight never departed, so it cannot be "0 minutes late"."""

    def test_delay_is_cleared_on_cancellation(self) -> None:
        from app.providers.normalization import clear_delay_if_cancelled

        assert clear_delay_if_cancelled(FlightStatus.CANCELLED, 0) is None
        assert clear_delay_if_cancelled(FlightStatus.CANCELLED, 45) is None
        assert clear_delay_if_cancelled(FlightStatus.DIVERTED, 0) is None

    def test_other_statuses_keep_their_delay(self) -> None:
        from app.providers.normalization import clear_delay_if_cancelled

        assert clear_delay_if_cancelled(FlightStatus.DEPARTED, 40) == 40
        assert clear_delay_if_cancelled(FlightStatus.SCHEDULED, 0) == 0
        assert clear_delay_if_cancelled(FlightStatus.DELAYED, 60) == 60
        assert clear_delay_if_cancelled(FlightStatus.DEPARTED, None) is None
