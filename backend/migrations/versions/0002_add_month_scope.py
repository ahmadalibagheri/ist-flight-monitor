"""Add the MONTH value to the stat_scope enum.

Delay and cancellation analytics are grouped by month as well as by hour, day of
week and time-of-day period. ``stat_scope`` is a native PostgreSQL enum, so a new
member needs an explicit ``ALTER TYPE`` - Alembic's autogenerate does not detect
enum value additions.

Revision ID: 0002_month_scope
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_month_scope"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS keeps the migration idempotent across partial re-runs.
    op.execute("ALTER TYPE stat_scope ADD VALUE IF NOT EXISTS 'MONTH'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type. Removing it means
    # rebuilding the type, which requires dropping every dependent column default
    # and rewriting both statistics tables. Rows using MONTH are deleted so the
    # value is unused, but the enum member itself is left in place - it is inert.
    op.execute(
        sa.text("DELETE FROM daily_statistics WHERE scope = 'MONTH'")
    )
    op.execute(
        sa.text("DELETE FROM hourly_statistics WHERE scope = 'MONTH'")
    )
