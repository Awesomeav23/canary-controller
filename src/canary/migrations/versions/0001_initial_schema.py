"""Initial schema: rollouts, their event log, deploy freezes (Weeks 1-2)

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Defined here, not imported from canary.models: a migration must keep describing the
# schema as it was, even after the models change.
Timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "rollouts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app", sa.String(63), nullable=False),
        sa.Column("image", sa.String(255), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_started_at", Timestamp, nullable=True),
        sa.Column("active_app", sa.String(63), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.Column("updated_at", Timestamp, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rollouts"),
        sa.UniqueConstraint("active_app", name="uq_rollouts_active_app"),
    )
    op.create_index("ix_rollouts_app", "rollouts", ["app"])
    op.create_index("ix_rollouts_state", "rollouts", ["state"])

    op.create_table(
        "rollout_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rollout_id", sa.Integer(), nullable=False),
        sa.Column("at", Timestamp, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_rollout_events"),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["rollouts.id"], name="fk_rollout_events_rollout_id_rollouts"
        ),
    )
    op.create_index("ix_rollout_events_rollout_id", "rollout_events", ["rollout_id"])

    op.create_table(
        "deploy_freezes",
        sa.Column("app", sa.String(63), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.PrimaryKeyConstraint("app", name="pk_deploy_freezes"),
    )


def downgrade() -> None:
    # Dropping a table drops its indexes. Don't drop them first: MySQL refuses to drop
    # an index a foreign key still uses (error 1553). SQLite allows it, so only the
    # MySQL CI job caught this.
    op.drop_table("deploy_freezes")
    op.drop_table("rollout_events")
    op.drop_table("rollouts")
