"""active client interaction table

Revision ID: 11e9528e2fb5
Revises: 44bbab5d1f05
Create Date: 2026-08-13 09:24:07.118455

Manual FA bookkeeping against one active-client digest line: a logged
call, a snooze, or a dismiss. Nothing here drives any automated send or
routing decision -- the active-client population has no campaign path at
all yet, so this is a record of what a human already did, not a trigger
for what the system does next.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "11e9528e2fb5"
down_revision: str | Sequence[str] | None = "44bbab5d1f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "active_client_interaction",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('call_logged', 'snoozed', 'dismissed')",
            name="ck_active_client_interaction_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_active_client_interaction_client_fund",
        "active_client_interaction",
        ["client_id", "unit_fund_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_active_client_interaction_client_fund", table_name="active_client_interaction"
    )
    op.drop_table("active_client_interaction")
