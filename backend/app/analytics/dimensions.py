"""Grouping facts into the dimensions the spec asks for.

One place decides what "by airline" or "by time of day" means, so the aggregator,
the API and the report renderer cannot drift apart.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from app.analytics.metrics import FlightFact, MetricBlock, compute_metrics
from app.analytics.reliability import compute_reliability
from app.core.config import settings
from app.core.enums import TIME_PERIODS, StatScope, period_for_hour

#: How each scope derives its key from a fact. ``None`` keys are dropped.
KEY_FUNCTIONS: dict[StatScope, Callable[[FlightFact], str | None]] = {
    StatScope.OVERALL: lambda f: "ALL",
    StatScope.ROUTE: lambda f: f.route,
    StatScope.AIRLINE: lambda f: f.airline_key,
    StatScope.FLIGHT: lambda f: f.flight_number,
    StatScope.PERIOD: lambda f: period_for_hour(f.hour_local),
    StatScope.DOW: lambda f: str(f.dow_local),
    StatScope.MONTH: lambda f: f.flight_date_local.strftime("%Y-%m"),
    StatScope.ROUTE_PERIOD: lambda f: f"{f.route}|{period_for_hour(f.hour_local)}",
    StatScope.ROUTE_AIRLINE: (
        lambda f: f"{f.route}|{f.airline_key}" if f.airline_key else None
    ),
}

#: Minimum sample before a scope's entries may be *ranked* against each other.
MIN_SAMPLE_BY_SCOPE: dict[StatScope, Callable[[], int]] = {
    StatScope.AIRLINE: lambda: settings.min_sample_size_airline,
    StatScope.ROUTE_AIRLINE: lambda: settings.min_sample_size_airline,
    StatScope.FLIGHT: lambda: settings.min_sample_size_flight,
    StatScope.PERIOD: lambda: settings.min_sample_size_period,
    StatScope.ROUTE_PERIOD: lambda: settings.min_sample_size_period,
    StatScope.DOW: lambda: settings.min_sample_size_period,
    StatScope.MONTH: lambda: settings.min_sample_size_period,
}


def min_sample_for(scope: StatScope) -> int:
    getter = MIN_SAMPLE_BY_SCOPE.get(scope)
    return getter() if getter else 1


def group_facts(
    facts: list[FlightFact], scope: StatScope
) -> dict[str, list[FlightFact]]:
    """Bucket facts by a scope's key."""
    key_of = KEY_FUNCTIONS[scope]
    grouped: dict[str, list[FlightFact]] = defaultdict(list)
    for fact in facts:
        key = key_of(fact)
        if key is None:
            continue
        grouped[key].append(fact)
    return dict(grouped)


def metrics_by_scope(
    facts: list[FlightFact], scope: StatScope
) -> dict[str, MetricBlock]:
    """Compute a metric block per key, including its reliability score."""
    minimum = min_sample_for(scope)
    out: dict[str, MetricBlock] = {}
    for key, group in group_facts(facts, scope).items():
        block = compute_metrics(group, min_sample_size=minimum)
        block.reliability_score = compute_reliability(
            block, min_sample_size=minimum
        ).score
        out[key] = block
    return out


def route_for_scope(scope: StatScope, key: str) -> str | None:
    """Extract the route a scope key belongs to, when it encodes one."""
    if scope is StatScope.ROUTE:
        return key
    if scope in {StatScope.ROUTE_PERIOD, StatScope.ROUTE_AIRLINE}:
        return key.split("|", 1)[0]
    return None


def period_labels() -> list[str]:
    return [label for label, _, _ in TIME_PERIODS]
