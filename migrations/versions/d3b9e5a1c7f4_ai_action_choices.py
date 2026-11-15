from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3b9e5a1c7f4"
down_revision: str | Sequence[str] | None = "b6d2f8a4c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_active_client_interaction_type"
TABLE = "active_client_interaction"
PROPOSAL_FK = "fk_active_client_interaction_proposal_id"
PROPOSAL_CLIENT_INDEX = "ix_agent_proposal_client_client_fund"

OLD_TYPES = "'call_logged', 'snoozed', 'dismissed', 'email_sent', 'flagged_for_account_manager'"
NEW_TYPES = f"{OLD_TYPES}, 'ai_action_taken', 'ai_action_set_aside'"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"type IN ({NEW_TYPES})")
    op.add_column(TABLE, sa.Column("proposal_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        PROPOSAL_FK, TABLE, "agent_proposal", ["proposal_id"], ["proposal_id"], ondelete="SET NULL"
    )
    op.create_index(PROPOSAL_CLIENT_INDEX, "agent_proposal_client", ["client_id", "unit_fund_id"])


def downgrade() -> None:
    op.drop_index(PROPOSAL_CLIENT_INDEX, table_name="agent_proposal_client")
    op.drop_constraint(PROPOSAL_FK, TABLE, type_="foreignkey")
    op.drop_column(TABLE, "proposal_id")
    op.execute(f"DELETE FROM {TABLE} WHERE type IN ('ai_action_taken', 'ai_action_set_aside')")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"type IN ({OLD_TYPES})")
