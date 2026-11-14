"""Add rollouts.baseline: last trusted stable readings (Week 3)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: existing rows simply have no baseline yet. Safe to run while
    # the old code is still serving, because the old code never reads this column.
    op.add_column("rollouts", sa.Column("baseline", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rollouts") as batch:
        batch.drop_column("baseline")
