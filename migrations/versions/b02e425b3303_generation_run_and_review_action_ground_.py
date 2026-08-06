"""generation run and review action ground truth labels

Revision ID: b02e425b3303
Revises: 70a714205301
Create Date: 2026-08-05 12:34:48.302274

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b02e425b3303"
down_revision: str | Sequence[str] | None = "70a714205301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generation_runs", sa.Column("priority_tier", sa.Text(), nullable=True))
    op.create_index(
        "ix_generation_runs_priority_tier", "generation_runs", ["priority_tier"], unique=False
    )

    op.add_column("review_action", sa.Column("message_angle", sa.Text(), nullable=True))
    op.add_column("review_action", sa.Column("priority_tier", sa.Text(), nullable=True))
    op.add_column(
        "review_action",
        sa.Column("edit_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_review_action_message_angle", "review_action", ["message_angle"], unique=False
    )
    op.create_index(
        "ix_review_action_priority_tier", "review_action", ["priority_tier"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_review_action_priority_tier", table_name="review_action")
    op.drop_index("ix_review_action_message_angle", table_name="review_action")
    op.drop_column("review_action", "edit_diff")
    op.drop_column("review_action", "priority_tier")
    op.drop_column("review_action", "message_angle")

    op.drop_index("ix_generation_runs_priority_tier", table_name="generation_runs")
    op.drop_column("generation_runs", "priority_tier")
