from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e1a7d9b3f2"
down_revision: str | Sequence[str] | None = "e8a2c6f4b1d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPE_CONSTRAINT = "ck_active_client_interaction_type"
OUTCOME_CONSTRAINT = "ck_active_client_interaction_outcome"
TABLE = "active_client_interaction"

OLD_TYPES = (
    "'call_logged', 'snoozed', 'dismissed', 'email_sent', 'flagged_for_account_manager', "
    "'ai_action_taken', 'ai_action_set_aside'"
)
NEW_TYPES = f"{OLD_TYPES}, 'meeting_logged', 'whatsapp_logged'"


def upgrade() -> None:
    op.drop_constraint(TYPE_CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(TYPE_CONSTRAINT, TABLE, f"type IN ({NEW_TYPES})")
    op.add_column(TABLE, sa.Column("outcome", sa.Text(), nullable=True))
    op.create_check_constraint(
        OUTCOME_CONSTRAINT, TABLE, "outcome IN ('connected', 'follow_up', 'no_answer')"
    )


def downgrade() -> None:
    op.drop_constraint(OUTCOME_CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, "outcome")
    op.execute(f"DELETE FROM {TABLE} WHERE type IN ('meeting_logged', 'whatsapp_logged')")
    op.drop_constraint(TYPE_CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(TYPE_CONSTRAINT, TABLE, f"type IN ({OLD_TYPES})")
