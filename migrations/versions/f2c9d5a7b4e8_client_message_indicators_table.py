"""client_message_indicators table

Revision ID: f2c9d5a7b4e8
Revises: e1a4c8b6d3f5
Create Date: 2026-07-25 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c9d5a7b4e8"
down_revision: str | Sequence[str] | None = "e1a4c8b6d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_message_indicators",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_angle", sa.Text(), nullable=False),
        sa.Column("urgency", sa.Text(), nullable=False),
        sa.Column("priority_tier", sa.Text(), nullable=False),
        sa.Column("prompt_variant", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=True),
        sa.Column("rule_name", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.ForeignKeyConstraint(["rule_id"], ["business_rules.rule_id"]),
        sa.PrimaryKeyConstraint("client_id"),
    )


def downgrade() -> None:
    op.drop_table("client_message_indicators")
