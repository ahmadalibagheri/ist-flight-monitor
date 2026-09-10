"""Multi-provider enrichment: per-field provenance and a wider data_source.

In merge mode ``flights.data_source`` records every contributing provider
("aerodatabox+opensky"), which no longer fits in 40 characters, and
``field_sources`` records which provider supplied each individual field. Without
that map a merged row is unauditable - you cannot tell whose gate number is on
screen, or which source to blame when two disagree.

Revision ID: 0004_merge_provenance
Revises: 0003_raw_payload
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_merge_provenance"
down_revision: str | None = "0003_raw_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "flights",
        "data_source",
        existing_type=sa.String(length=40),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
    op.add_column(
        "flights",
        sa.Column("field_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("flights", "field_sources")
    # Truncate any merged labels that no longer fit before narrowing the column.
    op.execute("UPDATE flights SET data_source = left(data_source, 40)")
    op.alter_column(
        "flights",
        "data_source",
        existing_type=sa.String(length=120),
        type_=sa.String(length=40),
        existing_nullable=False,
    )
