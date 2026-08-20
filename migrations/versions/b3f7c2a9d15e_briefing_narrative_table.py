"""briefing narrative table

Revision ID: b3f7c2a9d15e
Revises: a4c8e1f6b9d3
Create Date: 2026-08-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f7c2a9d15e"
down_revision: str | Sequence[str] | None = "a4c8e1f6b9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "briefing_narrative",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("narrative_text", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("fact_block_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id"),
    )


def downgrade() -> None:
    op.drop_table("briefing_narrative")
