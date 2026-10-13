from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e1b9d3f6"
down_revision: str | Sequence[str] | None = "e8b3c5d1f7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOWED_TIERS = (
    "'gradual_depositor', 'gradual_depositor_withdrawer', 'frequent_depositor', "
    "'frequent_depositor_withdrawer', 'no_transactions_in_review_period', "
    "'below_capital_preservation_threshold'"
)


def upgrade() -> None:
    thresholds = op.create_table(
        "client_tier_thresholds",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("review_period_days", sa.Integer(), nullable=False),
        sa.Column("recent_purchase_days", sa.Integer(), nullable=False),
        sa.Column("capital_preservation_threshold_kes", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("version"),
    )
    op.create_table(
        "client_tier",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("purchased_recently", sa.Boolean(), nullable=False),
        sa.Column("real_withdrawer", sa.Boolean(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("segment", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("thresholds_version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("client_id"),
        sa.ForeignKeyConstraint(["thresholds_version"], ["client_tier_thresholds.version"]),
        sa.CheckConstraint(f"tier in ({_ALLOWED_TIERS})", name="ck_client_tier_tier"),
        sa.CheckConstraint(
            "segment in ('active_active', 'active_dormant')", name="ck_client_tier_segment"
        ),
    )
    op.create_index("ix_client_tier_tier", "client_tier", ["tier"])
    op.bulk_insert(
        thresholds,
        [
            {
                "version": 1,
                "review_period_days": 365,
                "recent_purchase_days": 90,
                "capital_preservation_threshold_kes": 6000.0,
                "valid_from": date(2026, 10, 2),
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_client_tier_tier", table_name="client_tier")
    op.drop_table("client_tier")
    op.drop_table("client_tier_thresholds")
