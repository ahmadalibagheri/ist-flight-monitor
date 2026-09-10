"""Statistics and report response models."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import MetricsOut, RankedOut, ReliabilityOut


class SummaryOut(MetricsOut):
    """Metrics for one window, with the score broken down."""

    window: str
    start_date: date | None = None
    end_date: date | None = None
    route: str | None = None
    airline: str | None = None
    bucket_percentages: dict[str, float] = Field(
        default_factory=dict,
        description="Share of measurable flights delayed beyond each bucket, 0-1.",
    )
    reliability: ReliabilityOut | None = None


class HourlyPointOut(MetricsOut):
    hour_local: int
    stat_date: date | None = None


class PeriodOut(RankedOut):
    """One time-of-day bucket."""


class TimeOfDayOut(BaseModel):
    route: str
    best_period: str | None = None
    worst_delay_period: str | None = None
    worst_cancellation_period: str | None = None
    note: str | None = None
    periods: list[PeriodOut]


class DailyPointOut(MetricsOut):
    stat_date: date
    route: str | None = None


class TrendOut(BaseModel):
    route: str
    window_days: int
    current_period: dict[str, str]
    previous_period: dict[str, str]
    current: dict[str, float | int | None]
    previous: dict[str, float | int | None]
    deltas: dict[str, float | int | None]
    sufficient_data: bool


class FlightIssueOut(BaseModel):
    flight_number: str
    airline: str
    scheduled_local: str
    destination: str
    status: str
    delay_minutes: int | None = None
    reason: str | None = None


class RouteBlockOut(MetricsOut):
    route: str
    destination_name: str
    route_name: str | None = Field(
        None, description='Directional label, e.g. "TEHRAN -> ISTANBUL".'
    )
    departed: int
    scheduled_remaining: int


class HourlyReportOut(BaseModel):
    generated_at_utc: datetime
    generated_at_local: str
    local_date: date
    history_days: int
    routes: list[RouteBlockOut]
    cancelled: list[FlightIssueOut]
    delayed: list[FlightIssueOut]
    period_verdicts: list[TimeOfDayOut]
    best_flight: RankedOut | None = None
    worst_flight: RankedOut | None = None
    repeat_cancellations: list[dict[str, object]] = Field(default_factory=list)
    data_note: str | None = None
    text: str | None = Field(None, description="Rendered plain-text form.")


class HistoricalReportOut(BaseModel):
    generated_at_utc: datetime
    generated_at_local: str
    days: int
    start_date: date
    end_date: date
    overall: dict[str, float | int | None] = {}
    routes: list[dict[str, object]] = []
    airlines: list[dict[str, object]] = []
    trends: list[dict[str, object]] = []
    data_note: str | None = None
    text: str | None = None


class CollectionRunOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    provider: str | None = None
    trigger: str
    flights_seen: int
    flights_created: int
    flights_updated: int
    observations_written: int
    events_written: int
    cancellations_detected: int
    delays_detected: int
    stale_marked: int
    success: bool
    error: str | None = None

    model_config = {"from_attributes": True}
