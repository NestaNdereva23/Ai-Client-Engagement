from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e1a7b238"
down_revision: str | Sequence[str] | None = "b7f2a4d9c631"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign",
        sa.Column("planning_mode", sa.Text(), server_default="fixed_steps", nullable=False),
    )
    op.create_check_constraint(
        "ck_campaign_planning_mode", "campaign", "planning_mode IN ('fixed_steps', 'agent')"
    )
    op.execute("UPDATE campaign SET planning_mode = 'agent' WHERE agent_touch_policy IS NOT NULL")


def downgrade() -> None:
    op.execute(
        "UPDATE campaign SET agent_touch_policy = COALESCE(agent_touch_policy, '{}'::jsonb) "
        "WHERE planning_mode = 'agent'"
    )
    op.drop_constraint("ck_campaign_planning_mode", "campaign", type_="check")
    op.drop_column("campaign", "planning_mode")
