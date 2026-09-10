"""Track the originally advertised departure time.

Providers overwrite ``scheduledTime`` when an airline re-times a flight. Delay
measured against the current schedule then reads ~0 however far the flight moved:
a live example on IST-IKA shifted 315 minutes and scored as perfectly on time,
lifting the headline reliability score to 90/100. Retaining the first advertised
time makes that displacement measurable.

Existing rows are backfilled from the append-only observation history, which
already held the answer.

Revision ID: 0005_first_schedule
Revises: 0004_merge_provenance
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_first_schedule"
down_revision: str | None = "0004_merge_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "flights",
        sa.Column("first_scheduled_departure_utc", sa.DateTime(timezone=True), nullable=True),
    )
    # The observation history is append-only, so the earliest scheduled time each
    # flight was ever reported with is already on record.
    op.execute(
        """
        UPDATE flights f
        SET first_scheduled_departure_utc = sub.first_seen_schedule
        FROM (
            SELECT flight_id, MIN(scheduled_departure_utc) AS first_seen_schedule
            FROM flight_observations
            WHERE scheduled_departure_utc IS NOT NULL
            GROUP BY flight_id
        ) AS sub
        WHERE f.id = sub.flight_id
        """
    )
    # Flights with no usable observation fall back to their current schedule, so the
    # column means "earliest known" rather than being null for historical rows.
    op.execute(
        "UPDATE flights SET first_scheduled_departure_utc = scheduled_departure_utc "
        "WHERE first_scheduled_departure_utc IS NULL"
    )


def downgrade() -> None:
    op.drop_column("flights", "first_scheduled_departure_utc")
