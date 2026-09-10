"""Sent-alert ledger. Existence of a row is what prevents duplicate alerts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import AlertType

AlertEnum = SAEnum(AlertType, name="alert_type", native_enum=True, validate_strings=True)


class SentAlert(Base):
    """A Telegram message we have already delivered.

    ``dedup_key`` is a deterministic fingerprint of *what* was announced (for
    example ``CANCELLATION:TK878:2026-09-05``). The unique constraint makes
    duplicate suppression a database guarantee rather than an in-memory one, so
    it survives restarts and concurrent workers.
    """

    __tablename__ = "sent_alerts"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_sent_alerts_dedup_key"),
        Index("ix_alerts_type_time", "alert_type", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_type: Mapped[AlertType] = mapped_column(AlertEnum, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False)
    flight_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[str | None] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SentAlert {self.alert_type} {self.dedup_key}>"
