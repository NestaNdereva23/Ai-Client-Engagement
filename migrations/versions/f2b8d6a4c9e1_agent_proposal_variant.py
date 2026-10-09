from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8d6a4c9e1"
down_revision: str | Sequence[str] | None = "e4a7c2b9d513"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_proposal_variant",
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("content_mix", sa.Text(), nullable=True),
        sa.CheckConstraint("variant IN ('A', 'B')", name="ck_agent_proposal_variant_label"),
        sa.CheckConstraint(
            "content_mix IS NULL OR content_mix IN "
            "('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
            name="ck_agent_proposal_variant_content_mix",
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["agent_proposal.proposal_id"]),
        sa.PrimaryKeyConstraint("proposal_id", "variant"),
    )


def downgrade() -> None:
    op.drop_table("agent_proposal_variant")
