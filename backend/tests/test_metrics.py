"""Delay and cancellation metric computation, including the denominator rules."""

from __future__ import annotations

from app.analytics.metrics import compute_metrics


def test_counts_split_correctly(sample_facts) -> None:
    m = compute_metrics(sample_facts, delay_threshold=15)
    assert m.total_flights == 25
    assert m.cancelled_flights == 4
    assert m.unknown_flights == 1          # provider gave no timings
    assert m.measurable_flights == 20      # 25 - 4 cancelled - 1 unknown
    assert m.delayed_flights == 8
    assert m.on_time_flights == 12


def test_cancellation_rate_denominator_is_all_flights(sample_facts) -> None:
    m = compute_metrics(sample_facts)
    assert m.cancellation_rate == 4 / 25


def test_delay_rate_denominator_excludes_unknown_and_cancelled(sample_facts) -> None:
    """A flight with no timing data must not be counted as on time."""
    m = compute_metrics(sample_facts)
    assert m.delay_rate == 8 / 20
    assert m.on_time_rate == 12 / 20
    # If unknowns were wrongly treated as on-time the rate would be 12/21.
    assert m.on_time_rate != 12 / 21


def test_unknown_delay_is_excluded_from_the_average(make_fact) -> None:
    facts = [make_fact(delay=30), make_fact(delay=None), make_fact(delay=30)]
    m = compute_metrics(facts)
    assert m.avg_delay_minutes == 30.0
    assert m.unknown_flights == 1


def test_cancelled_flights_do_not_contribute_a_delay(make_fact) -> None:
    facts = [make_fact(delay=60), make_fact(cancelled=True)]
    m = compute_metrics(facts)
    assert m.avg_delay_minutes == 60.0
    assert m.measurable_flights == 1


def test_delay_buckets_are_strictly_greater_than(make_fact) -> None:
    facts = [make_fact(delay=d) for d in (10, 15, 16, 31, 61, 121)]
    m = compute_metrics(facts, buckets=[15, 30, 60, 120])
    assert m.delayed_gt_15 == 4   # 16, 31, 61, 121
    assert m.delayed_gt_30 == 3   # 31, 61, 121
    assert m.delayed_gt_60 == 2   # 61, 121
    assert m.delayed_gt_120 == 1  # 121


def test_early_departures_count_as_zero_lateness_but_keep_raw_max(make_fact) -> None:
    facts = [make_fact(delay=-20), make_fact(delay=40)]
    m = compute_metrics(facts)
    assert m.avg_delay_minutes == 20.0  # (0 + 40) / 2
    assert m.max_delay_minutes == 40


def test_distribution_statistics(make_fact) -> None:
    facts = [make_fact(delay=d) for d in (0, 10, 20, 30, 100)]
    m = compute_metrics(facts)
    assert m.median_delay_minutes == 20.0
    assert m.max_delay_minutes == 100
    assert m.p90_delay_minutes == 72.0  # interpolated between 30 and 100


def test_empty_population_is_all_none_not_zero() -> None:
    m = compute_metrics([])
    assert m.total_flights == 0
    assert m.cancellation_rate is None
    assert m.delay_rate is None
    assert m.avg_delay_minutes is None


def test_all_cancelled_has_no_delay_rate(make_fact) -> None:
    facts = [make_fact(cancelled=True) for _ in range(5)]
    m = compute_metrics(facts)
    assert m.cancellation_rate == 1.0
    assert m.delay_rate is None       # nothing measurable
    assert m.avg_delay_minutes is None


def test_sample_warning_set_below_minimum(make_fact) -> None:
    m = compute_metrics([make_fact(delay=5)], min_sample_size=20)
    assert m.sample_warning is not None
    assert "20 required" in m.sample_warning
