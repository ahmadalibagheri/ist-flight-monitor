"""Flight / airline reliability score, 0-100.

The score is a weighted sum of four normalised components. It is deliberately
simple and fully inspectable: :func:`score_breakdown` returns every component's
raw value, its normalised 0-1 form, its weight and its contribution, so a score
shown on the dashboard can always be explained.

    score = 100 * ( w_ot * on_time
                  + w_cx * (1 - min(cancel_rate / MAX_CANCEL, 1))
                  + w_ad * (1 - min(avg_delay  / MAX_DELAY,  1))
                  + w_sd * (1 - min(severe_rate / MAX_SEVERE, 1)) )

Every weight and every ``MAX_*`` anchor is an environment variable
(``SCORE_WEIGHT_*`` / ``SCORE_MAX_*``); the weights are validated to sum to 1.0 at
startup, so the output is always on a 0-100 scale.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.metrics import MetricBlock
from app.core.config import Settings
from app.core.config import settings as default_settings


@dataclass(slots=True, frozen=True)
class ScoreComponent:
    name: str
    raw_value: float | None
    normalised: float
    weight: float
    contribution: float
    explanation: str


@dataclass(slots=True, frozen=True)
class ReliabilityScore:
    #: ``None`` when nothing has been observed at all. A population of zero flights
    #: has *unknown* reliability, not bad reliability - reporting 0/100 there would
    #: read as "terrible" rather than "no data".
    score: float | None
    components: list[ScoreComponent]
    sample_size: int
    is_ranked: bool
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "sample_size": self.sample_size,
            "is_ranked": self.is_ranked,
            "warning": self.warning,
            "components": [
                {
                    "name": c.name,
                    "raw_value": c.raw_value,
                    "normalised": round(c.normalised, 4),
                    "weight": c.weight,
                    "contribution": round(c.contribution, 2),
                    "explanation": c.explanation,
                }
                for c in self.components
            ],
        }


def compute_reliability(
    metrics: MetricBlock,
    *,
    min_sample_size: int | None = None,
    config: Settings | None = None,
) -> ReliabilityScore:
    """Score a metric block.

    Two distinct "bad" cases are kept apart:

    * **Nothing observed** (``total_flights == 0``) -> ``score is None``. Reliability
      is unknown, and callers render it as "no data".
    * **Observed but unmeasurable** (for example every flight cancelled) -> a real
      low score. That is genuine information, not missing data, and an unmeasured
      component still scores 0 rather than earning a flattering default.
    """
    cfg = config or default_settings
    minimum = (
        min_sample_size if min_sample_size is not None else cfg.min_sample_size_flight
    )
    sample = metrics.total_flights
    is_ranked = sample >= minimum and metrics.measurable_flights > 0

    on_time = metrics.on_time_rate
    cancel_rate = metrics.cancellation_rate
    avg_delay = metrics.avg_delay_minutes
    severe_rate = metrics.severe_delay_rate

    components = [
        _component(
            "on_time_rate",
            on_time,
            _direct(on_time),
            cfg.score_weight_on_time,
            "Share of measurable departures leaving within the delay threshold. "
            "Higher is better.",
        ),
        _component(
            "cancellation_rate",
            cancel_rate,
            _inverse(cancel_rate, cfg.score_max_cancellation_rate),
            cfg.score_weight_cancellation,
            f"Cancellations as a share of all scheduled flights, scored against a "
            f"{cfg.score_max_cancellation_rate:.0%} worst case.",
        ),
        _component(
            "avg_delay_minutes",
            avg_delay,
            _inverse(avg_delay, cfg.score_max_avg_delay_minutes),
            cfg.score_weight_avg_delay,
            f"Mean lateness scored against a {cfg.score_max_avg_delay_minutes:.0f}-minute "
            "worst case.",
        ),
        _component(
            "severe_delay_rate",
            severe_rate,
            _inverse(severe_rate, cfg.score_max_severe_delay_rate),
            cfg.score_weight_severe_delay,
            f"Share of flights delayed more than 60 minutes, scored against a "
            f"{cfg.score_max_severe_delay_rate:.0%} worst case.",
        ),
    ]

    if metrics.total_flights == 0:
        return ReliabilityScore(
            score=None,
            components=components,
            sample_size=0,
            is_ranked=False,
            warning="No flights observed; reliability cannot be computed.",
        )

    if metrics.completed_flights == 0:
        # Flights are on the board but none has actually happened yet. Three of the
        # four components are unknowable before departure, so any number here would
        # be an artefact of missing data rather than a verdict on reliability.
        # "Not cancelled yet" is not the same as "reliable".
        return ReliabilityScore(
            score=None,
            components=components,
            sample_size=metrics.total_flights,
            is_ranked=False,
            warning=(
                f"{metrics.total_flights} flight(s) observed but none has departed, "
                "arrived or been cancelled yet; reliability cannot be computed."
            ),
        )

    raw_score = round(sum(c.contribution for c in components), 1)
    score = max(0.0, min(100.0, raw_score))

    warning = None
    if not is_ranked:
        warning = (
            f"Sample of {sample} flights is below the minimum of {minimum}; "
            "score is indicative only and excluded from rankings."
        )

    return ReliabilityScore(
        score=score,
        components=components,
        sample_size=sample,
        is_ranked=is_ranked,
        warning=warning,
    )


def _component(
    name: str,
    raw: float | None,
    normalised: float,
    weight: float,
    explanation: str,
) -> ScoreComponent:
    return ScoreComponent(
        name=name,
        raw_value=None if raw is None else round(raw, 4),
        normalised=normalised,
        weight=weight,
        contribution=100 * weight * normalised,
        explanation=explanation,
    )


def _direct(value: float | None) -> float:
    """A rate where higher is better, already on 0-1."""
    if value is None:
        return 0.0
    return max(0.0, min(1.0, value))


def _inverse(value: float | None, worst: float) -> float:
    """A metric where lower is better, normalised against a configured worst case.

    ``None`` means unmeasured, which scores 0 rather than a free full mark.
    """
    if value is None:
        return 0.0
    if worst <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (value / worst)))
