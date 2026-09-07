"""Hold the exact list of clients a proposal considered, in or out.

This is what turns "22 removed" into a fact anyone can read back, with the
reason attached to each one that was left out.

Revision ID: 6c38ff3ab9d8
Revises: 8cd5c6cff6f9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6c38ff3ab9d8"
down_revision: str | Sequence[str] | None = "8cd5c6cff6f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_proposal_client",
        sa.Column("proposal_client_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("variant", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "included OR skip_reason IS NOT NULL",
            name="ck_agent_proposal_client_skip_reason_required",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["agent_proposal.proposal_id"],
            name="fk_agent_proposal_client_proposal_id",
        ),
        sa.PrimaryKeyConstraint("proposal_client_id"),
        sa.UniqueConstraint(
            "proposal_id",
            "client_id",
            "unit_fund_id",
            name="uq_agent_proposal_client_proposal_client_fund",
        ),
    )
    op.create_index(
        op.f("ix_agent_proposal_client_proposal_id"),
        "agent_proposal_client",
        ["proposal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_proposal_client_proposal_id"), table_name="agent_proposal_client")
    op.drop_table("agent_proposal_client")
