"""Shared response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """A page of results plus enough metadata to fetch the next one."""

    items: list[T]
    total: int = Field(description="Total rows matching the filter, ignoring paging.")
    limit: int
    offset: int
    has_more: bool


class MetricsOut(BaseModel):
    """The metric block shared by every statistics response."""

    total_flights: int
    completed_flights: int = 0
    cancelled_flights: int = 0
    diverted_flights: int = 0
    delayed_flights: int = 0
    on_time_flights: int = 0
    unknown_flights: int = Field(
        0, description="Flights with no measurable delay; excluded from delay rates."
    )
    measurable_flights: int = Field(
        0, description="Denominator for delay_rate and on_time_rate."
    )

    delayed_gt_15: int = 0
    delayed_gt_30: int = 0
    delayed_gt_60: int = 0
    delayed_gt_120: int = 0

    avg_delay_minutes: float | None = None
    median_delay_minutes: float | None = None
    p90_delay_minutes: float | None = None
    max_delay_minutes: int | None = None

    cancellation_rate: float | None = Field(
        None, description="Cancelled / all scheduled flights, 0-1."
    )
    delay_rate: float | None = Field(
        None, description="Delayed / measurable flights, 0-1."
    )
    on_time_rate: float | None = None
    severe_delay_rate: float | None = Field(
        None, description="Delayed over 60 minutes / measurable flights, 0-1."
    )
    reliability_score: float | None = Field(None, description="0-100.")
    sample_warning: str | None = None


class ScoreComponentOut(BaseModel):
    name: str
    raw_value: float | None
    normalised: float
    weight: float
    contribution: float
    explanation: str


class ReliabilityOut(BaseModel):
    score: float | None = Field(
        None, description="0-100, or null when nothing has been observed."
    )
    sample_size: int
    is_ranked: bool
    warning: str | None = None
    components: list[ScoreComponentOut]


class RankedOut(MetricsOut):
    key: str
    label: str
    sample_size: int
    is_ranked: bool
    warning: str | None = None


class HealthOut(BaseModel):
    status: str
    version: str
    environment: str
    timezone: str
    time_utc: datetime
    time_local: str


class ReadinessOut(BaseModel):
    ready: bool
    checks: dict[str, object]


class ProviderOut(BaseModel):
    name: str
    kind: str
    description: str
    configured: bool
    in_chain: bool
    chain_position: int | None = None
    is_healthy: bool | None = None
    consecutive_failures: int | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None


class RouteOut(BaseModel):
    route: str
    origin_iata: str
    destination_iata: str
    # The origin is named as well as the destination: on a return leg the
    # destination alone does not tell a reader which direction they are looking at.
    origin_name: str | None = None
    origin_city: str | None = None
    destination_name: str | None = None
    destination_city: str | None = None
    first_seen: date | None = None
    last_seen: date | None = None
    total_flights: int = 0


class AirlineOut(ORMModel):
    iata: str | None
    icao: str | None
    name: str
