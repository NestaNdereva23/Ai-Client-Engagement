"""agent_proposal: a metric breakdown behind the evidence sentence

Adds evidence_detail, a nullable JSONB column for groups where there is a
real metric behind the group's evidence (for example, deposit_size_falling's
decline split into thirds). Not every situation has one, so this stays
unpopulated for the rest.

Revision ID: f3a8c261d9b7
Revises: c244801a469d
Create Date: 2026-10-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a8c261d9b7"
down_revision: str | Sequence[str] | None = "c244801a469d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_proposal",
        sa.Column("evidence_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_proposal", "evidence_detail")
