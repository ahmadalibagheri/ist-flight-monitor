from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Airline(Base):
    """Airlines discovered from provider payloads.

    ``iata`` is the natural key we group statistics by; it is upserted on first
    sighting rather than maintained by hand.
    """

    __tablename__ = "airlines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    iata: Mapped[str | None] = mapped_column(String(3), unique=True, index=True)
    icao: Mapped[str | None] = mapped_column(String(4), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Airline {self.iata or self.icao or self.name}>"
