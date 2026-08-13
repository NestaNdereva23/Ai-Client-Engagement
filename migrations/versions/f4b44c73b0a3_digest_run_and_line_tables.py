"""digest run and line tables

Revision ID: f4b44c73b0a3
Revises: 82a27dc9d0e9
Create Date: 2026-08-12 15:56:53.434134

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b44c73b0a3"
down_revision: str | Sequence[str] | None = "82a27dc9d0e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digest_run",
        sa.Column("digest_run_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("risk_run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["risk_run_id"], ["risk_run.run_id"]),
        sa.PrimaryKeyConstraint("digest_run_id"),
    )
    op.create_index("ix_digest_run_risk_run_id", "digest_run", ["risk_run_id"], unique=False)
    op.create_table(
        "digest_line",
        sa.Column("line_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("digest_run_id", sa.BigInteger(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("group_total", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("risk_band", sa.Text(), nullable=False),
        sa.Column("risk_reasons", sa.Text(), nullable=False),
        sa.Column("aum_at_risk", sa.Float(), nullable=False),
        sa.Column("score_delta", sa.Integer(), nullable=True),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("in_call_queue", sa.Boolean(), nullable=False),
        sa.Column(
            "complaint_caveat", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["digest_run_id"], ["digest_run.digest_run_id"]),
        sa.PrimaryKeyConstraint("line_id"),
    )
    op.create_index(
        "ix_digest_line_run_group_rank",
        "digest_line",
        ["digest_run_id", "group_key", "rank"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_digest_line_run_group_rank", table_name="digest_line")
    op.drop_table("digest_line")
    op.drop_index("ix_digest_run_risk_run_id", table_name="digest_run")
    op.drop_table("digest_run")
