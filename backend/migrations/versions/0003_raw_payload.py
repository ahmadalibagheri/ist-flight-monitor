"""Store each provider response verbatim on the observation.

Historical provider responses cannot be re-fetched: no API will tell you what it
said about a flight last week. Any field not persisted at collection time is
therefore lost permanently. A diff of a live AeroDataBox payload against the
modelled columns showed eight fields being discarded on every observation -
including the provider's own ``quality`` signal, which says whether a timestamp is
live-tracked or merely scheduled.

Revision ID: 0003_raw_payload
Revises: 0002_month_scope
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_raw_payload"
down_revision: str | None = "0002_month_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "flight_observations",
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("flight_observations", "raw_payload")
