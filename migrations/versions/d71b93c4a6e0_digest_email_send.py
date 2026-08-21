"""digest_email_send

Revision ID: d71b93c4a6e0
Revises: c5a1d84e7f22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d71b93c4a6e0"
down_revision = "c5a1d84e7f22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest_email_send",
        sa.Column("send_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("digest_run_id", sa.BigInteger(), nullable=False),
        sa.Column("fa_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("client_count", sa.Integer(), nullable=False),
        sa.Column("fund_value_total", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["digest_run_id"], ["digest_run.digest_run_id"]),
        sa.PrimaryKeyConstraint("send_id"),
        sa.UniqueConstraint("digest_run_id", "fa_id", name="uq_digest_email_send_run_fa"),
    )
    op.create_index("ix_digest_email_send_digest_run_id", "digest_email_send", ["digest_run_id"])


def downgrade() -> None:
    op.drop_index("ix_digest_email_send_digest_run_id", table_name="digest_email_send")
    op.drop_table("digest_email_send")
