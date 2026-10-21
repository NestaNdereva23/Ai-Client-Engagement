"""version the agent permission table

Revision ID: 00c8b7dc51e7
Revises: c1e4a9f7b2d3
Create Date: 2026-10-21 15:43:26.027740

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "00c8b7dc51e7"
down_revision: str | Sequence[str] | None = "c1e4a9f7b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_permission", sa.Column("valid_from", sa.Date(), nullable=True))
    op.add_column("agent_permission", sa.Column("valid_to", sa.Date(), nullable=True))
    op.execute("UPDATE agent_permission SET valid_from = '2000-01-01' WHERE valid_from IS NULL")
    op.alter_column("agent_permission", "valid_from", nullable=False)
    op.drop_constraint(
        op.f("uq_agent_permission_action_tier_band"), "agent_permission", type_="unique"
    )
    op.create_index(
        "uq_agent_permission_action_tier_band_live",
        "agent_permission",
        ["action_code", "priority_tier", "risk_band"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_agent_permission_action_tier_band_live",
        table_name="agent_permission",
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.execute("DELETE FROM agent_permission WHERE valid_to IS NOT NULL")
    op.create_unique_constraint(
        op.f("uq_agent_permission_action_tier_band"),
        "agent_permission",
        ["action_code", "priority_tier", "risk_band"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_column("agent_permission", "valid_to")
    op.drop_column("agent_permission", "valid_from")
