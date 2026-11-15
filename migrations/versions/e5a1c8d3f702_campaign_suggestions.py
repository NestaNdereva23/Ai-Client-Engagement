from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a1c8d3f702"
down_revision: str | Sequence[str] | None = "c4d9e1a7b238"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KIND_CHECK = "agent_kind IN ('nightly', 'intelligence', 'action', 'chat', 'campaign')"
_NEW_KIND_CHECK = (
    "agent_kind IN ('nightly', 'intelligence', 'action', 'chat', 'campaign', "
    "'campaign_suggestions')"
)
_STATE_CHECK = (
    "suggestion_state IS NULL OR suggestion_state IN ('pending', 'approved', 'dismissed')"
)


def upgrade() -> None:
    op.add_column("campaign", sa.Column("proposed_by_run_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "campaign",
        sa.Column("suggestion", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("campaign", sa.Column("suggestion_state", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_campaign_proposed_by_run_id",
        "campaign",
        "agent_run",
        ["proposed_by_run_id"],
        ["run_id"],
    )
    op.create_index(op.f("ix_campaign_proposed_by_run_id"), "campaign", ["proposed_by_run_id"])
    op.create_check_constraint("ck_campaign_suggestion_state", "campaign", _STATE_CHECK)

    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", _NEW_KIND_CHECK)


def downgrade() -> None:
    op.execute(
        "UPDATE agent_run SET agent_kind = 'campaign' WHERE agent_kind = 'campaign_suggestions'"
    )
    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", _OLD_KIND_CHECK)

    op.drop_constraint("ck_campaign_suggestion_state", "campaign", type_="check")
    op.drop_index(op.f("ix_campaign_proposed_by_run_id"), table_name="campaign")
    op.drop_constraint("fk_campaign_proposed_by_run_id", "campaign", type_="foreignkey")
    op.drop_column("campaign", "suggestion_state")
    op.drop_column("campaign", "suggestion")
    op.drop_column("campaign", "proposed_by_run_id")
