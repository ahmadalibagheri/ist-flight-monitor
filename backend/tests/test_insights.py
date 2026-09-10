"""Time-of-day verdicts, airline ranking and hourly aggregation."""

from __future__ import annotations

from app.analytics.dimensions import metrics_by_scope
from app.analytics.insights import rank_airlines, rank_flights, time_of_day_verdict
from app.core.enums import StatScope, period_for_hour


class TestPeriodBucketing:
    def test_boundaries_are_half_open(self) -> None:
        assert period_for_hour(0) == "00:00-06:00"
        assert period_for_hour(5) == "00:00-06:00"
        assert period_for_hour(6) == "06:00-10:00"
        assert period_for_hour(9) == "06:00-10:00"
        assert period_for_hour(10) == "10:00-14:00"
        assert period_for_hour(18) == "18:00-22:00"
        assert period_for_hour(21) == "18:00-22:00"
        assert period_for_hour(22) == "22:00-00:00"
        assert period_for_hour(23) == "22:00-00:00"

    def test_every_hour_is_covered(self) -> None:
        assert len({period_for_hour(h) for h in range(24)}) == 6

    def test_invalid_hour_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            period_for_hour(24)


class TestTimeOfDayVerdict:
    def test_identifies_best_and_worst_periods(self, make_fact) -> None:
        facts = []
        # Morning block: punctual, never cancelled.
        for _ in range(20):
            facts.append(make_fact(delay=3, hour=8))
        # Evening block: heavily delayed and frequently cancelled.
        for _ in range(14):
            facts.append(make_fact(delay=95, hour=19))
        for _ in range(6):
            facts.append(make_fact(cancelled=True, hour=19))

        verdict = time_of_day_verdict(facts, "IST-IKA")
        assert verdict.best_period == "06:00-10:00"
        assert verdict.worst_delay_period == "18:00-22:00"
        assert verdict.worst_cancellation_period == "18:00-22:00"

    def test_worst_delay_and_worst_cancellation_can_differ(self, make_fact) -> None:
        """These are reported separately precisely because they diverge."""
        facts = []
        for _ in range(15):                       # 10-14: delays, no cancellations
            facts.append(make_fact(delay=80, hour=11))
        for _ in range(10):                       # 14-18: punctual but cancelled a lot
            facts.append(make_fact(delay=2, hour=15))
        for _ in range(8):
            facts.append(make_fact(cancelled=True, hour=15))
        for _ in range(15):                       # 06-10: clean
            facts.append(make_fact(delay=1, hour=8))

        verdict = time_of_day_verdict(facts, "IST-IKA")
        assert verdict.worst_delay_period == "10:00-14:00"
        assert verdict.worst_cancellation_period == "14:00-18:00"
        assert verdict.best_period == "06:00-10:00"

    def test_one_eligible_period_is_not_both_best_and_worst(self, make_fact) -> None:
        """Observed live: a single qualifying bucket was named best AND worst.

        Best/worst are comparative claims. With one period above the minimum, max()
        returns it for every answer, which reads as insight but is an artefact.
        """
        facts = [make_fact(delay=70, hour=23) for _ in range(9)]
        facts += [make_fact(cancelled=True, hour=23) for _ in range(3)]
        facts += [make_fact(delay=5, hour=8) for _ in range(4)]  # below the minimum

        verdict = time_of_day_verdict(facts, "IST-IKA")
        assert verdict.best_period is None
        assert verdict.worst_delay_period is None
        assert verdict.worst_cancellation_period is None
        assert verdict.note is not None
        assert "cannot be compared" in verdict.note
        # The data is still shown, just not ranked against anything.
        assert any(p.metrics.total_flights for p in verdict.periods)

    def test_two_eligible_periods_can_be_compared(self, make_fact) -> None:
        facts = [make_fact(delay=2, hour=8) for _ in range(12)]
        facts += [make_fact(delay=80, hour=23) for _ in range(12)]
        verdict = time_of_day_verdict(facts, "IST-IKA")
        assert verdict.best_period == "06:00-10:00"
        assert verdict.worst_delay_period == "22:00-00:00"
        assert verdict.best_period != verdict.worst_delay_period

    def test_thin_data_declines_to_rank(self, make_fact) -> None:
        facts = [make_fact(delay=5, hour=8) for _ in range(3)]
        verdict = time_of_day_verdict(facts, "IST-IKA")
        assert verdict.best_period is None
        assert verdict.note is not None
        assert "minimum" in verdict.note
        # The data is still shown, just not ranked.
        assert verdict.periods

    def test_unknown_route_is_reported_not_crashed(self, sample_facts) -> None:
        verdict = time_of_day_verdict(sample_facts, "IST-XXX")
        assert verdict.note == "No flights observed for this route yet."
        assert verdict.periods == []


class TestAirlineRanking:
    def test_small_samples_are_never_ranked_first(self, make_fact) -> None:
        """A carrier with two lucky flights must not top the table."""
        facts = []
        for _ in range(30):                       # solid but imperfect record
            facts.append(make_fact(delay=20, airline="TK"))
        for _ in range(2):                        # perfect, tiny sample
            facts.append(make_fact(delay=0, airline="XX"))

        ranking = rank_airlines(facts)
        assert ranking[0].key == "TK"
        assert ranking[0].is_ranked is True

        tiny = next(e for e in ranking if e.key == "XX")
        assert tiny.is_ranked is False
        assert ranking.index(tiny) > ranking.index(ranking[0])

    def test_better_airline_ranks_higher(self, make_fact) -> None:
        facts = []
        for _ in range(25):
            facts.append(make_fact(delay=2, airline="TK"))
        for _ in range(25):
            facts.append(make_fact(delay=90, airline="W5"))
        ranking = [e for e in rank_airlines(facts) if e.is_ranked]
        assert [e.key for e in ranking] == ["TK", "W5"]

    def test_carriers_without_an_iata_code_are_still_counted(self, make_fact) -> None:
        """Smaller operators publish ICAO only; they must not vanish from stats."""
        import dataclasses

        facts = [make_fact(delay=5, airline="TK") for _ in range(3)]
        icao_only = dataclasses.replace(
            facts[0], airline_iata=None, airline_icao="VRH", airline_name="Varesh"
        )
        name_only = dataclasses.replace(
            facts[0], airline_iata=None, airline_icao=None, airline_name="SAHA AIRLINES"
        )
        ranking = rank_airlines([*facts, icao_only, name_only])
        keys = {e.key for e in ranking}
        assert "VRH" in keys
        assert "SAHA AIRLINES" in keys
        assert sum(e.metrics.total_flights for e in ranking) == 5

    def test_labels_carry_the_airline_name(self, make_fact) -> None:
        facts = [make_fact(delay=5, airline="TK") for _ in range(25)]
        assert rank_airlines(facts)[0].label == "Turkish Airlines"

    def test_flights_without_an_airline_are_skipped(self, make_fact) -> None:
        """A payload with no carrier code cannot be attributed, so it is dropped."""
        import dataclasses

        facts = [make_fact(delay=5, airline="TK") for _ in range(3)]
        facts.append(dataclasses.replace(facts[0], airline_iata=None, airline_name=None))

        ranking = rank_airlines(facts)
        assert [e.key for e in ranking] == ["TK"]
        assert ranking[0].metrics.total_flights == 3


class TestFlightRanking:
    def test_ranks_individual_flight_numbers(self, make_fact) -> None:
        facts = []
        for _ in range(12):
            facts.append(make_fact(delay=2, flight_number="TK872"))
        for _ in range(12):
            facts.append(make_fact(delay=110, flight_number="TK878"))
        ranking = [e for e in rank_flights(facts) if e.is_ranked]
        assert ranking[0].key == "TK872"
        assert ranking[-1].key == "TK878"
        assert ranking[0].score > ranking[-1].score


class TestHourlyAggregation:
    def test_groups_by_local_hour(self, make_fact) -> None:
        facts = [make_fact(delay=10, hour=8) for _ in range(5)]
        facts += [make_fact(delay=70, hour=19) for _ in range(3)]

        from collections import defaultdict

        from app.analytics.metrics import compute_metrics

        by_hour = defaultdict(list)
        for fact in facts:
            by_hour[fact.hour_local].append(fact)

        assert compute_metrics(by_hour[8]).total_flights == 5
        assert compute_metrics(by_hour[8]).delayed_flights == 0
        assert compute_metrics(by_hour[19]).delayed_flights == 3

    def test_route_scope_keys_are_well_formed(self, make_fact) -> None:
        facts = [make_fact(delay=5, destination="IKA"), make_fact(delay=5, destination="MHD")]
        keys = set(metrics_by_scope(facts, StatScope.ROUTE))
        assert keys == {"IST-IKA", "IST-MHD"}

    def test_route_period_scope_encodes_both(self, make_fact) -> None:
        facts = [make_fact(delay=5, destination="IKA", hour=19)]
        assert set(metrics_by_scope(facts, StatScope.ROUTE_PERIOD)) == {"IST-IKA|18:00-22:00"}


class TestMonthAndDayOfWeekScopes:
    """Spec sections 9 and 10 require delay and cancellation breakdowns by month."""

    def test_month_scope_groups_by_local_calendar_month(self, make_fact) -> None:
        from datetime import date

        facts = [make_fact(delay=10, day=date(2026, 8, 15)) for _ in range(4)]
        facts += [make_fact(delay=90, day=date(2026, 9, 3)) for _ in range(6)]

        grouped = metrics_by_scope(facts, StatScope.MONTH)
        assert set(grouped) == {"2026-08", "2026-09"}
        assert grouped["2026-08"].total_flights == 4
        assert grouped["2026-09"].delayed_flights == 6

    def test_month_scope_separates_cancellations(self, make_fact) -> None:
        from datetime import date

        facts = [make_fact(cancelled=True, day=date(2026, 8, 2)) for _ in range(3)]
        facts += [make_fact(delay=5, day=date(2026, 8, 2)) for _ in range(7)]
        grouped = metrics_by_scope(facts, StatScope.MONTH)
        assert grouped["2026-08"].cancellation_rate == 0.3

    def test_day_of_week_scope_uses_iso_numbering(self, make_fact) -> None:
        from datetime import date

        # 2026-09-07 is a Monday.
        facts = [make_fact(delay=5, day=date(2026, 9, 7)) for _ in range(3)]
        assert set(metrics_by_scope(facts, StatScope.DOW)) == {"1"}


class TestRepeatCancellations:
    """The sample-size guard must not hide a flight that never operates."""

    def test_flight_cancelled_every_day_is_reported(self, make_fact) -> None:
        from datetime import date

        from app.analytics.insights import repeat_cancellations

        facts = [
            make_fact(cancelled=True, flight_number="IRZ8257", hour=23,
                      day=date(2026, 9, d))
            for d in (7, 8, 9)
        ]
        # A healthy flight for contrast, also below the ranking threshold.
        facts += [
            make_fact(delay=5, flight_number="TK878", day=date(2026, 9, d))
            for d in (7, 8, 9)
        ]

        report = repeat_cancellations(facts)
        assert [r["flight_number"] for r in report] == ["IRZ8257"]
        entry = report[0]
        assert entry["cancellations"] == 3
        assert entry["scheduled_occasions"] == 3
        assert entry["cancellation_rate"] == 1.0
        assert entry["dates"] == ["2026-09-07", "2026-09-08", "2026-09-09"]

    def test_a_single_cancellation_is_not_a_pattern(self, make_fact) -> None:
        from app.analytics.insights import repeat_cancellations

        assert repeat_cancellations([make_fact(cancelled=True)]) == []

    def test_ranked_leaderboard_still_respects_the_minimum(self, make_fact) -> None:
        """The threshold stays intact where it belongs - this is a separate report."""
        from datetime import date

        facts = [
            make_fact(cancelled=True, flight_number="IRZ8257", day=date(2026, 9, d))
            for d in (7, 8, 9)
        ]
        assert all(not e.is_ranked for e in rank_flights(facts))

    def test_worst_offenders_sort_first(self, make_fact) -> None:
        from datetime import date

        from app.analytics.insights import repeat_cancellations

        facts = [
            make_fact(cancelled=True, flight_number="AAA1", day=date(2026, 9, d))
            for d in (7, 8, 9)
        ]
        facts += [
            make_fact(cancelled=True, flight_number="BBB2", day=date(2026, 9, d))
            for d in (7, 8)
        ]
        assert [r["flight_number"] for r in repeat_cancellations(facts)] == ["AAA1", "BBB2"]
