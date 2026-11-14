"""Add rollouts.traffic_shifted_at: when a step's traffic actually moved (Week 5)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13

Started from `alembic revision --autogenerate`, then edited after review:
- added the backfill (autogenerate only sees schema, not data)
- batch mode for the downgrade, so it also works on SQLite
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.add_column("rollouts", sa.Column("traffic_shifted_at", Timestamp, nullable=True))
    # Before this change traffic always moved the moment a step started. Without the
    # backfill, in-flight rollouts would read NULL as "waiting for pods" and restart
    # their current step's pause.
    op.execute(
        "UPDATE rollouts SET traffic_shifted_at = step_started_at WHERE step_started_at IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("rollouts") as batch:
        batch.drop_column("traffic_shifted_at")
