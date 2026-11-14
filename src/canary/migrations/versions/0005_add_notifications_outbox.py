"""Add notifications: transactional outbox for Slack/webhook alerts (Week 6)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13

Started from `alembic revision --autogenerate`, then edited after review: the generated
downgrade dropped ix_notifications_rollout_id before the table, which MySQL rejects
because the foreign key needs it (same bug as 0001, error 1553).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rollout_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(20), nullable=False),
        sa.Column("sink", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", Timestamp, nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.Column("sent_at", Timestamp, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["rollouts.id"], name="fk_notifications_rollout_id_rollouts"
        ),
    )
    # The dispatcher's query: pending rows whose next attempt is due.
    op.create_index("ix_notifications_due", "notifications", ["status", "next_attempt_at"])
    op.create_index("ix_notifications_rollout_id", "notifications", ["rollout_id"])


def downgrade() -> None:
    op.drop_table("notifications")  # drops its indexes too
