"""Reliability scoring: bounds, monotonicity, transparency and sample guarding."""

from __future__ import annotations

from app.analytics.metrics import MetricBlock, compute_metrics
from app.analytics.reliability import compute_reliability
from app.core.config import Settings
from app.core.enums import FlightStatus


def _block(**kwargs) -> MetricBlock:
    defaults = {
        "total_flights": 100,
        # Settled outcomes: reliability is only computable once flights have happened.
        "completed_flights": 100,
        "measurable_flights": 100,
        "on_time_rate": 0.8,
        "cancellation_rate": 0.05,
        "avg_delay_minutes": 20.0,
        "severe_delay_rate": 0.1,
    }
    defaults.update(kwargs)
    return MetricBlock(**defaults)


def test_score_is_bounded_0_to_100() -> None:
    perfect = compute_reliability(
        _block(on_time_rate=1.0, cancellation_rate=0.0, avg_delay_minutes=0.0, severe_delay_rate=0.0)
    )
    worst = compute_reliability(
        _block(on_time_rate=0.0, cancellation_rate=1.0, avg_delay_minutes=600.0, severe_delay_rate=1.0)
    )
    assert perfect.score == 100.0
    assert worst.score == 0.0


def test_components_sum_to_the_score() -> None:
    result = compute_reliability(_block())
    assert round(sum(c.contribution for c in result.components), 1) == result.score


def test_every_component_is_explained() -> None:
    """The score has to be defensible on a dashboard, so nothing is opaque."""
    result = compute_reliability(_block())
    assert {c.name for c in result.components} == {
        "on_time_rate",
        "cancellation_rate",
        "avg_delay_minutes",
        "severe_delay_rate",
    }
    for component in result.components:
        assert component.explanation
        assert 0.0 <= component.normalised <= 1.0


def test_more_cancellations_lower_the_score() -> None:
    good = compute_reliability(_block(cancellation_rate=0.01)).score
    bad = compute_reliability(_block(cancellation_rate=0.20)).score
    assert good > bad


def test_longer_delays_lower_the_score() -> None:
    assert compute_reliability(_block(avg_delay_minutes=5)).score > compute_reliability(
        _block(avg_delay_minutes=80)
    ).score


def test_unmeasured_metrics_score_zero_not_full_marks() -> None:
    """Absent data must never be rewarded."""
    result = compute_reliability(
        _block(on_time_rate=None, avg_delay_minutes=None, severe_delay_rate=None,
               cancellation_rate=None)
    )
    assert result.score == 0.0


def test_small_samples_are_flagged_and_unranked() -> None:
    result = compute_reliability(_block(total_flights=3), min_sample_size=20)
    assert result.is_ranked is False
    assert result.warning is not None
    assert "below the minimum" in result.warning


def test_weights_are_configurable() -> None:
    """Reweighting to care only about cancellations changes the outcome."""
    config = Settings(
        score_weight_on_time=0.0,
        score_weight_cancellation=1.0,
        score_weight_avg_delay=0.0,
        score_weight_severe_delay=0.0,
    )
    result = compute_reliability(_block(cancellation_rate=0.0), config=config)
    assert result.score == 100.0


def test_weights_must_sum_to_one() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must sum to 1.0"):
        Settings(
            score_weight_on_time=0.9,
            score_weight_cancellation=0.9,
            score_weight_avg_delay=0.1,
            score_weight_severe_delay=0.1,
        )


def test_realistic_scenario_from_the_specification(make_fact) -> None:
    """The spec's worked example: 78% on-time, 3.2% cancelled, 24 min, 7% severe."""
    facts = []
    for _ in range(78):
        facts.append(make_fact(delay=5))
    for _ in range(15):
        facts.append(make_fact(delay=40))
    for _ in range(7):
        facts.append(make_fact(delay=90))
    for _ in range(3):
        facts.append(make_fact(cancelled=True))

    metrics = compute_metrics(facts)
    result = compute_reliability(metrics, min_sample_size=10)
    assert result.is_ranked
    assert 70 <= result.score <= 90


class TestNoDataIsNotZero:
    """"Nothing observed" and "observed and terrible" must not look the same."""

    def test_empty_population_scores_none(self) -> None:
        result = compute_reliability(compute_metrics([]))
        assert result.score is None
        assert result.is_ranked is False
        assert result.warning is not None
        assert "cannot be computed" in result.warning

    def test_pending_flights_score_none(self, make_fact) -> None:
        """Flights that have not happened yet cannot be scored."""
        from app.core.enums import FlightStatus

        facts = [
            make_fact(delay=None, status=FlightStatus.SCHEDULED) for _ in range(5)
        ]
        result = compute_reliability(compute_metrics(facts), min_sample_size=1)
        assert result.score is None
        assert result.is_ranked is False
        assert "none has departed" in (result.warning or "")

    def test_a_single_completed_flight_is_enough_to_score(self, make_fact) -> None:
        from app.core.enums import FlightStatus

        facts = [make_fact(delay=None, status=FlightStatus.SCHEDULED) for _ in range(4)]
        facts.append(make_fact(delay=5, status=FlightStatus.DEPARTED))
        result = compute_reliability(compute_metrics(facts), min_sample_size=1)
        assert result.score is not None

    def test_flights_without_timing_score_none_not_thirty(self, make_fact) -> None:
        """Observed live on a newly-enabled route: 2 flights, no timings, "30/100".

        Only the cancellation component can be scored, so the weighted total lands
        near 30 and reads as poor reliability. A route the provider reports without
        timings must not look worse than one that is genuinely late.
        """
        from app.core.enums import FlightStatus

        facts = [
            make_fact(delay=None, status=FlightStatus.DEPARTED),
            make_fact(delay=None, status=FlightStatus.UNKNOWN),
        ]
        metrics = compute_metrics(facts)
        assert metrics.measurable_flights == 0

        result = compute_reliability(metrics, min_sample_size=1)
        assert result.score is None
        assert result.is_ranked is False
        assert "usable timing" in (result.warning or "")

    def test_cancellations_alone_are_still_measurable(self, make_fact) -> None:
        """A cancellation needs no timing to be a real outcome, so it is scored."""
        facts = [make_fact(cancelled=True) for _ in range(3)]
        facts += [make_fact(delay=None, status=FlightStatus.DEPARTED) for _ in range(2)]
        result = compute_reliability(compute_metrics(facts), min_sample_size=1)
        assert result.score is not None, "cancellations are knowable without timings"

    def test_all_cancelled_earns_a_real_low_score(self, make_fact) -> None:
        facts = [make_fact(cancelled=True) for _ in range(30)]
        result = compute_reliability(compute_metrics(facts), min_sample_size=10)
        assert result.score == 0.0  # a measured verdict, not missing data
        assert result.is_ranked is False  # nothing measurable to rank on

    def test_ranking_never_puts_an_unknown_score_first(self, make_fact) -> None:
        from app.analytics.insights import rank_airlines

        facts = [make_fact(delay=5, airline="TK") for _ in range(25)]
        ranking = rank_airlines(facts)
        assert ranking[0].score is not None
