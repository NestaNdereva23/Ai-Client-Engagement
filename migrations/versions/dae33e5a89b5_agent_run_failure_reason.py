"""Why a run ended in the failed state, in plain words.

A run that stops midway needs a reason a person can read back later; state
alone only says it failed, not what happened.

Revision ID: dae33e5a89b5
Revises: eb88fd1aa4fd
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dae33e5a89b5"
down_revision: str | Sequence[str] | None = "eb88fd1aa4fd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("failure_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run", "failure_reason")
