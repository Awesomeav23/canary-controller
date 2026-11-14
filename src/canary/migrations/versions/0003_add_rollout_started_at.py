"""Add rollouts.started_at: when the canary first got traffic (Week 4)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.add_column("rollouts", sa.Column("started_at", Timestamp, nullable=True))
    # Backfill: for rollouts that already got traffic, the first "step" event is when.
    op.execute(
        """
        UPDATE rollouts SET started_at = (
            SELECT MIN(e.at) FROM rollout_events e
            WHERE e.rollout_id = rollouts.id AND e.kind = 'step'
        )
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("rollouts") as batch:
        batch.drop_column("started_at")
