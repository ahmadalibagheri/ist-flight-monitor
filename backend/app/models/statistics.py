"""Materialised analytics.

Both tables share one shape: a *dimension* (what is being measured) plus a fixed
metric block. Keeping the metric columns identical means the report layer and the
API can treat "delay stats by airline" and "delay stats by hour" with the same code.

Rows are recomputed idempotently by :mod:`app.analytics.aggregator`; they are a
cache over ``flight_observations`` and can always be rebuilt from it.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import StatScope

ScopeEnum = SAEnum(StatScope, name="stat_scope", native_enum=True, validate_strings=True)


class _MetricsMixin:
    """The metric block shared by every statistics table."""

    total_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Flights whose outcome is known (departed/arrived/cancelled/diverted).
    completed_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diverted_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Flights with a *measurable* delay above the configured threshold.
    delayed_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    on_time_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Flights we could not classify because the provider gave no usable timing.
    unknown_flights: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    delayed_gt_15: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delayed_gt_30: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delayed_gt_60: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delayed_gt_120: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    avg_delay_minutes: Mapped[float | None] = mapped_column(Float)
    median_delay_minutes: Mapped[float | None] = mapped_column(Float)
    p90_delay_minutes: Mapped[float | None] = mapped_column(Float)
    max_delay_minutes: Mapped[int | None] = mapped_column(Integer)

    cancellation_rate: Mapped[float | None] = mapped_column(Float)
    delay_rate: Mapped[float | None] = mapped_column(Float)
    on_time_rate: Mapped[float | None] = mapped_column(Float)
    severe_delay_rate: Mapped[float | None] = mapped_column(Float)
    reliability_score: Mapped[float | None] = mapped_column(Float)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DailyStatistic(Base, _MetricsMixin):
    """One row per (local date, dimension)."""

    __tablename__ = "daily_statistics"
    __table_args__ = (
        UniqueConstraint("stat_date", "scope", "scope_key", name="uq_daily_stat_dimension"),
        Index("ix_daily_scope_date", "scope", "scope_key", "stat_date"),
        Index("ix_daily_date", "stat_date"),
        Index("ix_daily_route", "route"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: Local (Europe/Istanbul by default) calendar date of scheduled departure.
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    scope: Mapped[StatScope] = mapped_column(ScopeEnum, nullable=False)
    #: Dimension value: route key, airline IATA, flight number, or period label.
    #: ``"ALL"`` for the OVERALL scope.
    scope_key: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Denormalised route so route-scoped filters work on every scope.
    route: Mapped[str | None] = mapped_column(String(8))


class HourlyStatistic(Base, _MetricsMixin):
    """One row per (local date, local hour, dimension)."""

    __tablename__ = "hourly_statistics"
    __table_args__ = (
        UniqueConstraint(
            "stat_date", "hour_local", "scope", "scope_key", name="uq_hourly_stat_dimension"
        ),
        Index("ix_hourly_scope_date", "scope", "scope_key", "stat_date"),
        Index("ix_hourly_hour", "hour_local"),
        Index("ix_hourly_date", "stat_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    hour_local: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    scope: Mapped[StatScope] = mapped_column(ScopeEnum, nullable=False)
    scope_key: Mapped[str] = mapped_column(String(40), nullable=False)
    route: Mapped[str | None] = mapped_column(String(8))
