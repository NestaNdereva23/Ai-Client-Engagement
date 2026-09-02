"""message_template guardrail_rejected status

Revision ID: ea46ff789505
Revises: d6f3b9e2a4c8
Create Date: 2026-09-01 16:10:30.525663

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ea46ff789505"
down_revision: str | Sequence[str] | None = "d6f3b9e2a4c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_message_template_status", "message_template", type_="check")
    op.create_check_constraint(
        "ck_message_template_status",
        "message_template",
        "status IN ('pending_review', 'approved', 'rejected', 'escalated', 'held', "
        "'guardrail_rejected')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE message_template SET status = 'rejected' WHERE status = 'guardrail_rejected'"
    )
    op.drop_constraint("ck_message_template_status", "message_template", type_="check")
    op.create_check_constraint(
        "ck_message_template_status",
        "message_template",
        "status IN ('pending_review', 'approved', 'rejected', 'escalated', 'held')",
    )
