"""Multi-provider merge rules.

These pin the four documented rules in :mod:`app.providers.merge`. Getting them
wrong produces plausible-looking but wrong statistics, which is worse than an
obvious failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.enums import FlightStatus, ProviderKind
from app.providers.base import NormalizedFlight
from app.providers.merge import merge_all, merge_records

TRUST = ["aerodatabox", "aviationstack", "opensky"]
SCHEDULED = datetime(2026, 9, 6, 5, 15, tzinfo=UTC)


def record(source: str, **overrides) -> NormalizedFlight:
    defaults = {
        "flight_number": "TK878",
        "origin_iata": "IST",
        "destination_iata": "IKA",
        "scheduled_departure_utc": SCHEDULED,
        "data_source": source,
        "observed_at": datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return NormalizedFlight(**defaults)


class TestRule1TrustOrder:
    def test_higher_trust_provider_wins_a_contested_field(self) -> None:
        high = record("aerodatabox", gate="F7", terminal="I")
        low = record("aviationstack", gate="B12", terminal="D")
        merged, sources, _ = merge_records([low, high], TRUST)
        assert merged.gate == "F7"
        assert sources["gate"] == "aerodatabox"

    def test_lower_trust_fills_a_gap_the_leader_left(self) -> None:
        """The whole point of enrichment."""
        high = record("aerodatabox", gate=None, aircraft_type=None, terminal="I")
        low = record("aviationstack", gate="B12", aircraft_type="B77W")
        merged, sources, _ = merge_records([high, low], TRUST)
        assert merged.terminal == "I"          # from the leader
        assert merged.gate == "B12"            # filled in
        assert merged.aircraft_type == "B77W"  # filled in
        assert sources["gate"] == "aviationstack"
        assert sources["terminal"] == "aerodatabox"

    def test_input_order_does_not_matter_only_trust_does(self) -> None:
        a = record("aerodatabox", gate="F7")
        b = record("aviationstack", gate="B12")
        assert merge_records([a, b], TRUST)[0].gate == "F7"
        assert merge_records([b, a], TRUST)[0].gate == "F7"


class TestRule2ObservedBeatsPredicted:
    def test_actual_time_from_a_low_trust_source_is_used(self) -> None:
        """A free ADS-B feed reporting a real wheels-up beats a paid estimate."""
        actual = SCHEDULED.replace(hour=6, minute=25)
        high = record("aerodatabox", estimated_departure_utc=SCHEDULED.replace(hour=5, minute=45))
        low = record("opensky", actual_departure_utc=actual)
        merged, sources, _ = merge_records([high, low], TRUST)
        assert merged.actual_departure_utc == actual
        assert sources["actual_departure_utc"] == "opensky"
        # And the status follows the evidence, not the leader's stale text.
        assert merged.status is FlightStatus.DEPARTED

    def test_disagreeing_actual_times_are_reported_as_a_conflict(self) -> None:
        a = record("aerodatabox", actual_departure_utc=SCHEDULED.replace(hour=6, minute=0))
        b = record("opensky", actual_departure_utc=SCHEDULED.replace(hour=7, minute=30))
        merged, _, conflicts = merge_records([a, b], TRUST)
        assert merged.actual_departure_utc.hour == 6  # trust order breaks the tie
        assert any("actual_departure_utc" in c for c in conflicts)


class TestRule3DerivedStatusAndDelay:
    def test_delay_is_recomputed_from_merged_timings(self) -> None:
        """Not taken from whichever provider happened to state one."""
        high = record("aerodatabox", delay_minutes=0)
        low = record("opensky", actual_departure_utc=SCHEDULED.replace(hour=6, minute=25))
        merged, sources, _ = merge_records([high, low], TRUST)
        assert merged.delay_minutes == 70  # 05:15 -> 06:25
        assert sources["delay_minutes"] == "derived"

    def test_status_cannot_contradict_the_merged_times(self) -> None:
        """A SCHEDULED flight with a real departure time is an incoherent row."""
        stale = record("aerodatabox", raw_status="Expected", status=FlightStatus.SCHEDULED)
        live = record("opensky", actual_departure_utc=SCHEDULED.replace(hour=5, minute=30))
        merged, _, _ = merge_records([stale, live], TRUST)
        assert merged.status is FlightStatus.DEPARTED
        assert merged.actual_departure_utc is not None

    def test_unknown_delay_stays_unknown_after_merging(self) -> None:
        a = record("aerodatabox")
        b = record("aviationstack")
        merged, _, _ = merge_records([a, b], TRUST)
        assert merged.delay_minutes is None


class TestRule4Cancellation:
    def test_trusted_provider_decides_a_disputed_cancellation(self) -> None:
        """A false cancellation is more damaging than a late one."""
        high = record("aerodatabox", is_cancelled=False, raw_status="Expected")
        low = record("aviationstack", is_cancelled=True, raw_status="cancelled")
        merged, _, conflicts = merge_records([low, high], TRUST)
        assert merged.is_cancelled is False
        assert merged.status is not FlightStatus.CANCELLED
        assert any("is_cancelled" in c for c in conflicts)

    def test_agreement_produces_no_conflict(self) -> None:
        a = record("aerodatabox", is_cancelled=True, raw_status="Canceled")
        b = record("aviationstack", is_cancelled=True, raw_status="cancelled")
        merged, _, conflicts = merge_records([a, b], TRUST)
        assert merged.is_cancelled is True
        assert conflicts == []


class TestMockQuarantineSurvivesMerging:
    def test_mock_records_never_enrich_real_ones(self) -> None:
        real = record("aerodatabox", gate=None)
        fake = record("mock", gate="Z99", provider_kind=ProviderKind.MOCK)
        merged, _, _ = merge_records([real, fake], TRUST)
        assert merged.gate is None, "synthetic data must not leak into a real record"
        assert merged.is_mock is False


class TestMergeAll:
    def test_groups_by_flight_and_reports_contributors(self) -> None:
        batch_a = [record("aerodatabox", gate="F7"), record("aerodatabox", flight_number="W5113")]
        batch_b = [record("aviationstack", aircraft_type="B77W")]
        merged, provenance, _ = merge_all([batch_a, batch_b], TRUST)

        assert len(merged) == 2
        tk = next(f for f in merged if f.flight_number == "TK878")
        assert tk.gate == "F7"
        assert tk.aircraft_type == "B77W"
        assert "aerodatabox" in tk.data_source and "aviationstack" in tk.data_source
        assert provenance[tk.dedup_key()]["gate"] == "aerodatabox"

    def test_a_single_provider_passes_through_unchanged(self) -> None:
        merged, _, conflicts = merge_all([[record("aerodatabox", gate="F7")]], TRUST)
        assert len(merged) == 1
        assert merged[0].data_source == "aerodatabox"
        assert conflicts == []

    def test_empty_input_is_not_an_error(self) -> None:
        merged, provenance, conflicts = merge_all([[], []], TRUST)
        assert merged == [] and provenance == {} and conflicts == []

    def test_merging_nothing_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one record"):
            merge_records([], TRUST)

    def test_unknown_provider_sorts_last(self) -> None:
        """A provider missing from the chain must not silently outrank the chain."""
        known = record("aerodatabox", gate="F7")
        unknown = record("someothersource", gate="Z1")
        merged, _, _ = merge_records([unknown, known], TRUST)
        assert merged.gate == "F7"
