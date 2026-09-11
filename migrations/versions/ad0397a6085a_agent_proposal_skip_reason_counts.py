"""How many client funds a proposal left out, tallied by reason.

A group that empties itself should say which check emptied it straight from
its own record, not only from a query across agent_proposal_client rows.

Revision ID: ad0397a6085a
Revises: dae33e5a89b5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ad0397a6085a"
down_revision: str | Sequence[str] | None = "dae33e5a89b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_proposal", sa.Column("skip_reason_counts", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agent_proposal", "skip_reason_counts")
