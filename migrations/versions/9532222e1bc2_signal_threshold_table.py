"""signal threshold table

Revision ID: 9532222e1bc2
Revises: b11ce06f7b30
Create Date: 2026-09-15 12:47:33.083977

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "9532222e1bc2"
down_revision: str | Sequence[str] | None = "b11ce06f7b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_VALID_FROM = date(2026, 9, 15)


def upgrade() -> None:
    op.create_table(
        "signal_threshold",
        sa.Column("threshold_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("signal_code", sa.Text(), nullable=False),
        sa.Column("threshold_name", sa.Text(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("threshold_id"),
        sa.UniqueConstraint(
            "version",
            "signal_code",
            "threshold_name",
            name="uq_signal_threshold_version_signal_code_threshold_name",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_signal_threshold_status",
        ),
    )
    op.create_index("ix_signal_threshold_version", "signal_threshold", ["version"], unique=False)

    seed = sa.table(
        "signal_threshold",
        sa.column("version", sa.Integer),
        sa.column("signal_code", sa.Text),
        sa.column("threshold_name", sa.Text),
        sa.column("value", sa.Float),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("status", sa.Text),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "signal_code": "first_deposit_recent",
                "threshold_name": "window_days",
                "value": 30,
                "valid_from": _SEED_VALID_FROM,
                "valid_to": None,
                "status": "published",
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_threshold_version", table_name="signal_threshold")
    op.drop_table("signal_threshold")
