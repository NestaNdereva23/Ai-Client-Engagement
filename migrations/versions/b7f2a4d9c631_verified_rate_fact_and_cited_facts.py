from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7f2a4d9c631"
down_revision: str | Sequence[str] | None = "f4a8d2c6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verified_rate_fact",
        sa.Column("fact_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("rate_value", sa.Numeric(6, 3), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fact_id"),
    )
    op.create_index(op.f("ix_verified_rate_fact_product"), "verified_rate_fact", ["product"])

    op.add_column(
        "touch_plan",
        sa.Column("cited_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("touch_plan", "cited_facts")

    op.drop_index(op.f("ix_verified_rate_fact_product"), table_name="verified_rate_fact")
    op.drop_table("verified_rate_fact")
