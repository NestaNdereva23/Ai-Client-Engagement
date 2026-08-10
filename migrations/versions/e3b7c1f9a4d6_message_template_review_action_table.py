"""message_template_review_action table

Revision ID: e3b7c1f9a4d6
Revises: d8a1f4c6e9b3
Create Date: 2026-08-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e3b7c1f9a4d6"
down_revision: str | Sequence[str] | None = "d8a1f4c6e9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_template_review_action",
        sa.Column("review_action_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("edited_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("message_angle", sa.Text(), nullable=True),
        sa.Column("priority_tier", sa.Text(), nullable=True),
        sa.Column("edit_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('approve', 'edit_approve', 'reject', 'escalate', 'hold')",
            name="ck_message_template_review_action_outcome",
        ),
        sa.ForeignKeyConstraint(["template_id"], ["message_template.template_id"]),
        sa.PrimaryKeyConstraint("review_action_id"),
    )
    op.create_index(
        "ix_message_template_review_action_template_id",
        "message_template_review_action",
        ["template_id"],
    )
    op.create_index(
        "ix_message_template_review_action_message_angle",
        "message_template_review_action",
        ["message_angle"],
    )
    op.create_index(
        "ix_message_template_review_action_priority_tier",
        "message_template_review_action",
        ["priority_tier"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_template_review_action_priority_tier",
        table_name="message_template_review_action",
    )
    op.drop_index(
        "ix_message_template_review_action_message_angle",
        table_name="message_template_review_action",
    )
    op.drop_index(
        "ix_message_template_review_action_template_id",
        table_name="message_template_review_action",
    )
    op.drop_table("message_template_review_action")
