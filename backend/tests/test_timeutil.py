"""Timezone handling.

Istanbul has been permanently UTC+3 since 2016, so the fixed-offset expectations
below are stable. The DST-sensitive helper (:func:`local_day_bounds`) is tested
against a zone that does still change, to prove it is not doing naive arithmetic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.timeutil import (
    ensure_utc,
    local_date,
    local_day_bounds,
    local_dow,
    local_hour,
    minutes_between,
    parse_iso,
    to_local,
)

ISTANBUL = ZoneInfo("Europe/Istanbul")


def test_naive_datetime_is_rejected() -> None:
    """A naive datetime is a bug, not something to guess a zone for."""
    with pytest.raises(ValueError, match="naive datetime"):
        ensure_utc(datetime(2026, 9, 6, 8, 0))


def test_utc_stored_value_renders_in_local_time() -> None:
    stored = datetime(2026, 9, 6, 5, 30, tzinfo=UTC)
    local = to_local(stored, ISTANBUL)
    assert local is not None
    assert (local.hour, local.minute) == (8, 30)


def test_local_projections_use_local_not_utc() -> None:
    """A 23:30 UTC departure is 02:30 the *next* local day."""
    stored = datetime(2026, 9, 6, 23, 30, tzinfo=UTC)
    assert local_date(stored, ISTANBUL) == date(2026, 9, 7)
    assert local_hour(stored, ISTANBUL) == 2
    assert local_dow(stored, ISTANBUL) == 1  # Monday


def test_local_day_bounds_span_exactly_one_local_day() -> None:
    start, end = local_day_bounds(date(2026, 9, 6), ISTANBUL)
    assert start == datetime(2026, 9, 5, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    assert (end - start).total_seconds() == 24 * 3600


def test_local_day_bounds_handle_a_dst_transition() -> None:
    """Derived from the next local midnight, so a 23-hour day stays correct."""
    berlin = ZoneInfo("Europe/Berlin")
    start, end = local_day_bounds(date(2026, 3, 29), berlin)  # spring forward
    assert (end - start).total_seconds() == 23 * 3600


def test_minutes_between_normalises_offsets() -> None:
    later = datetime(2026, 9, 6, 11, 45, tzinfo=ISTANBUL)  # 08:45 UTC
    earlier = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    assert minutes_between(later, earlier) == 45


def test_minutes_between_is_none_when_either_side_missing() -> None:
    assert minutes_between(None, datetime(2026, 9, 6, 8, 0, tzinfo=UTC)) is None
    assert minutes_between(datetime(2026, 9, 6, 8, 0, tzinfo=UTC), None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-06T05:15:00Z", datetime(2026, 9, 6, 5, 15, tzinfo=UTC)),
        ("2026-09-06 05:15Z", datetime(2026, 9, 6, 5, 15, tzinfo=UTC)),
        ("2026-09-06 08:15+03:00", datetime(2026, 9, 6, 5, 15, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_iso_handles_provider_formats(raw: str | None, expected: datetime | None) -> None:
    parsed = parse_iso(raw)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert parsed.astimezone(UTC) == expected
