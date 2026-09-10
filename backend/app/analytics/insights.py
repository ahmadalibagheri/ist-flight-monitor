"""Derived answers: best/worst periods, airline rankings, reliability leaderboards
and period-over-period trends.

This is the layer that turns metric blocks into the statements the hourly report
and the dashboard actually make ("best time to fly IST-IKA is 06:00-10:00").
Every ranking here respects the configured minimum sample size - a route with
three observed flights never wins or loses a leaderboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.analytics.dimensions import metrics_by_scope, min_sample_for, period_labels
from app.analytics.metrics import FlightFact, MetricBlock, compute_metrics
from app.analytics.queries import facts_for_local_dates, local_today
from app.analytics.reliability import compute_reliability
from app.core.config import settings
from app.core.enums import StatScope


@dataclass(slots=True)
class RankedEntry:
    key: str
    label: str
    metrics: MetricBlock
    score: float | None
    sample_size: int
    is_ranked: bool
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload = dict(self.metrics.as_columns())
        payload.update(
            {
                "key": self.key,
                "label": self.label,
                "reliability_score": self.score,
                "sample_size": self.sample_size,
                "is_ranked": self.is_ranked,
                "warning": self.warning,
                "measurable_flights": self.metrics.measurable_flights,
            }
        )
        return payload


@dataclass(slots=True)
class PeriodVerdict:
    """Best/worst time-of-day answer for one route."""

    route: str
    best_period: str | None = None
    worst_delay_period: str | None = None
    worst_cancellation_period: str | None = None
    periods: list[RankedEntry] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "best_period": self.best_period,
            "worst_delay_period": self.worst_delay_period,
            "worst_cancellation_period": self.worst_cancellation_period,
            "note": self.note,
            "periods": [entry.as_dict() for entry in self.periods],
        }


def rank(
    facts: list[FlightFact],
    scope: StatScope,
    *,
    labeller: dict[str, str] | None = None,
) -> list[RankedEntry]:
    """Score every key in a scope, best first. Under-sampled keys sort last."""
    minimum = min_sample_for(scope)
    entries: list[RankedEntry] = []
    for key, block in metrics_by_scope(facts, scope).items():
        score = compute_reliability(block, min_sample_size=minimum)
        entries.append(
            RankedEntry(
                key=key,
                label=(labeller or {}).get(key, key),
                metrics=block,
                score=score.score,
                sample_size=block.total_flights,
                is_ranked=score.is_ranked,
                warning=score.warning,
            )
        )
    # Ranked entries first, then by score descending, then by sample size.
    # An unknown score sorts as -1 so it can never outrank a measured one.
    entries.sort(
        key=lambda e: (e.is_ranked, e.score if e.score is not None else -1.0, e.sample_size),
        reverse=True,
    )
    return entries


def rank_airlines(facts: list[FlightFact]) -> list[RankedEntry]:
    """Airline reliability leaderboard, honouring MIN_SAMPLE_SIZE_AIRLINE."""
    labels = {
        f.airline_key: (f.airline_name or f.airline_key)
        for f in facts
        if f.airline_key
    }
    return rank(facts, StatScope.AIRLINE, labeller=labels)


def rank_flights(facts: list[FlightFact]) -> list[RankedEntry]:
    """Per-flight-number reliability leaderboard."""
    return rank(facts, StatScope.FLIGHT)


#: A flight cancelled at least this often, every time it was scheduled, is reported
#: regardless of sample size.
REPEAT_CANCELLATION_MIN_OCCURRENCES = 2


def repeat_cancellations(facts: list[FlightFact]) -> list[dict[str, object]]:
    """Flight numbers cancelled on multiple separate days.

    The sample-size guard exists so a carrier with two lucky flights cannot top the
    leaderboard - but it cuts both ways, and was hiding the single most actionable
    fact in the data: a flight cancelled every day it was scheduled sits below the
    ranking threshold and appears nowhere.

    A repeated cancellation needs no statistical confidence. Three cancellations out
    of three scheduled days is not a small sample to be discounted; it is a pattern
    a traveller must be told about. Reported separately from the ranking so the
    minimum-sample rule stays intact where it belongs.
    """
    by_flight: dict[str, list[FlightFact]] = {}
    for fact in facts:
        by_flight.setdefault(fact.flight_number, []).append(fact)

    out: list[dict[str, object]] = []
    for number, group in by_flight.items():
        cancelled = [f for f in group if f.is_cancelled]
        if len(cancelled) < REPEAT_CANCELLATION_MIN_OCCURRENCES:
            continue
        days = sorted({f.flight_date_local for f in cancelled})
        out.append(
            {
                "flight_number": number,
                "airline": cancelled[0].airline_name or cancelled[0].airline_key,
                "route": cancelled[0].route,
                "scheduled_local_time": f"{cancelled[0].hour_local:02d}:00",
                "cancellations": len(cancelled),
                "scheduled_occasions": len(group),
                "dates": [d.isoformat() for d in days],
                # 1.0 means it has never once operated in the observed window.
                "cancellation_rate": round(len(cancelled) / len(group), 3),
            }
        )
    out.sort(key=lambda r: (r["cancellations"], r["cancellation_rate"]), reverse=True)  # type: ignore[arg-type,return-value]
    return out


def time_of_day_verdict(facts: list[FlightFact], route: str) -> PeriodVerdict:
    """Best and worst time-of-day windows for one route.

    "Best" maximises the reliability score; the two "worst" answers are reported
    separately because the period with the most delays is not always the period
    with the most cancellations.
    """
    route_facts = [f for f in facts if f.route == route]
    verdict = PeriodVerdict(route=route)
    if not route_facts:
        verdict.note = "No flights observed for this route yet."
        return verdict

    entries = rank(route_facts, StatScope.PERIOD)
    # Keep the canonical chronological order for display.
    order = {label: index for index, label in enumerate(period_labels())}
    verdict.periods = sorted(entries, key=lambda e: order.get(e.key, 99))

    minimum = min_sample_for(StatScope.PERIOD)
    eligible = [e for e in entries if e.is_ranked]
    if not eligible:
        verdict.note = (
            f"No time period has reached the {minimum}-flight minimum yet; periods are "
            "shown but not ranked."
        )
        return verdict

    # "Best" and "worst" are comparative claims, so they need at least two periods to
    # compare. With a single eligible bucket, max() would return it for all three
    # answers - naming the same window best *and* worst, which is not an insight but
    # an artefact of thin data.
    if len(eligible) < 2:
        only = eligible[0]
        verdict.note = (
            f"Only {only.key} has reached the {minimum}-flight minimum, so periods "
            "cannot be compared yet. Its own figures are shown below."
        )
        return verdict

    verdict.best_period = max(eligible, key=lambda e: e.score or 0.0).key

    with_delay = [e for e in eligible if e.metrics.delay_rate is not None]
    if with_delay:
        verdict.worst_delay_period = max(
            with_delay, key=lambda e: e.metrics.delay_rate or 0.0
        ).key

    with_cancel = [e for e in eligible if e.metrics.cancellation_rate is not None]
    if with_cancel:
        verdict.worst_cancellation_period = max(
            with_cancel, key=lambda e: e.metrics.cancellation_rate or 0.0
        ).key

    # A single period cannot be simultaneously the best and the worst.
    if verdict.best_period == verdict.worst_delay_period == verdict.worst_cancellation_period:
        verdict.note = (
            "Every ranked period scores alike, so no period stands out yet."
        )

    return verdict


def rolling_summary(
    session: Session, days: int, *, route: str | None = None, reference: date | None = None
) -> MetricBlock:
    """Metrics across a rolling window of local days."""
    end = reference or local_today()
    start = end - timedelta(days=days - 1)
    facts = facts_for_local_dates(session, start, end, route=route)
    block = compute_metrics(facts)
    block.reliability_score = compute_reliability(block, min_sample_size=1).score
    return block


def trend(
    session: Session,
    days: int,
    *,
    route: str | None = None,
    reference: date | None = None,
) -> dict[str, object]:
    """Compare the last ``days`` days with the ``days`` before them.

    Answers "what is changing over time?" - a positive ``delta`` on a rate means
    it got worse, since every rate here is one where lower is better except
    ``on_time_rate``.
    """
    end = reference or local_today()
    current_start = end - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)

    current = compute_metrics(
        facts_for_local_dates(session, current_start, end, route=route)
    )
    previous = compute_metrics(
        facts_for_local_dates(session, previous_start, previous_end, route=route)
    )

    def delta(attr: str) -> float | None:
        now = getattr(current, attr)
        before = getattr(previous, attr)
        if now is None or before is None:
            return None
        return round(now - before, 4)

    return {
        "route": route or "ALL",
        "window_days": days,
        "current_period": {"start": current_start.isoformat(), "end": end.isoformat()},
        "previous_period": {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
        },
        "current": current.as_columns(),
        "previous": previous.as_columns(),
        "deltas": {
            "total_flights": current.total_flights - previous.total_flights,
            "cancellation_rate": delta("cancellation_rate"),
            "delay_rate": delta("delay_rate"),
            "on_time_rate": delta("on_time_rate"),
            "avg_delay_minutes": delta("avg_delay_minutes"),
        },
        "sufficient_data": (
            current.total_flights >= settings.min_sample_size_period
            and previous.total_flights >= settings.min_sample_size_period
        ),
    }
