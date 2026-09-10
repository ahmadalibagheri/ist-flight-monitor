"""Collection-run bookkeeping and provider health tracking."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CollectionRun(Base):
    """One execution of the collector. Every observation links back to a run."""

    __tablename__ = "collection_runs"
    __table_args__ = (Index("ix_runs_started", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    provider: Mapped[str | None] = mapped_column(String(40))
    providers_attempted: Mapped[str | None] = mapped_column(String(200))
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduler")

    flights_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flights_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flights_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observations_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellations_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delays_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_marked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CollectionRun {self.id} {self.provider} success={self.success}>"


class ProviderHealth(Base):
    """Rolling health of each configured provider, used for failover decisions."""

    __tablename__ = "provider_health"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    is_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_failures: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    #: When set, the provider is skipped until this instant (rate-limit cooldown).
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ProviderHealth {self.provider} healthy={self.is_healthy}>"
