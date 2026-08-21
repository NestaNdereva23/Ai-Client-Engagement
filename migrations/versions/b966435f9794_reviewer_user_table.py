"""reviewer user table

Adds reviewer_user, the login for the reviewer console (V10.2). The other
diffs alembic's autogenerate detected against the live schema (client_fund
index, generation_cost_config_version constraint naming, the
outreach_message/review_cohort FK and unique constraint) predate this
change and are left alone here; they belong to whichever migration
actually owns that drift, not this one.

Revision ID: b966435f9794
Revises: ff42810be366
Create Date: 2026-08-20 12:27:21.888690

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b966435f9794"
down_revision: str | Sequence[str] | None = "ff42810be366"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "reviewer_user",
        sa.Column("user_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('fa', 'reviewer', 'team_lead', 'admin', 'relationship_manager')",
            name="ck_reviewer_user_role",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(op.f("ix_reviewer_user_username"), "reviewer_user", ["username"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_reviewer_user_username"), table_name="reviewer_user")
    op.drop_table("reviewer_user")
