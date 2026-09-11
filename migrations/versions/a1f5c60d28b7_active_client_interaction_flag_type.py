"""Allow a flag raised for an account manager as an interaction type.

Revision ID: a1f5c60d28b7
Revises: f3a90d27c614
"""

from __future__ import annotations

from alembic import op

revision = "a1f5c60d28b7"
down_revision = "f3a90d27c614"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_active_client_interaction_type"
TABLE = "active_client_interaction"

OLD_TYPES = "'call_logged', 'snoozed', 'dismissed', 'email_sent'"
NEW_TYPES = f"{OLD_TYPES}, 'flagged_for_account_manager'"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"type IN ({NEW_TYPES})")


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE type = 'flagged_for_account_manager'")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"type IN ({OLD_TYPES})")
