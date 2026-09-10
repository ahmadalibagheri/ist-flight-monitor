"""Delay, cancellation, time-of-day and reliability analytics."""

from app.analytics.aggregator import aggregate_day, aggregate_recent, rebuild_range, summarise
from app.analytics.insights import (
    PeriodVerdict,
    RankedEntry,
    rank_airlines,
    rank_flights,
    repeat_cancellations,
    rolling_summary,
    time_of_day_verdict,
    trend,
)
from app.analytics.metrics import FlightFact, MetricBlock, compute_metrics
from app.analytics.reliability import ReliabilityScore, compute_reliability

__all__ = [
    "FlightFact",
    "MetricBlock",
    "PeriodVerdict",
    "RankedEntry",
    "ReliabilityScore",
    "aggregate_day",
    "aggregate_recent",
    "compute_metrics",
    "compute_reliability",
    "rank_airlines",
    "rank_flights",
    "rebuild_range",
    "repeat_cancellations",
    "rolling_summary",
    "summarise",
    "time_of_day_verdict",
    "trend",
]
