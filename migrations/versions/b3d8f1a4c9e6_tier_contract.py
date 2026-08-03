"""tier_contract table and the four tier policies

Revision ID: b3d8f1a4c9e6
Revises: a1c7f3e9b6d2
Create Date: 2026-08-03 19:00:00.000000

Email is the primary channel on every tier, unlike the source report's
per-tier channel split: one delivery path and one opt-out mechanism reach
every relationship. valid_from matches the rule set this contract serves,
so neither goes live before the other.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d8f1a4c9e6"
down_revision: str | Sequence[str] | None = "a1c7f3e9b6d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 12, 1)

# tier, display_name, primary_channel, secondary_channel, max_words, sign_off,
# human_approval, review_sample_rate
_TIERS = [
    ("T1", "Tier 1 top", "email", "call_brief", 120, "named relationship manager", True, 1.0),
    ("T2", "Tier 2 high", "email", None, 140, "named person", False, 0.1),
    ("T3", "Tier 3 medium", "email", None, 110, "client services", False, 0.02),
    ("T4", "Tier 4 low", "email", "batch", 60, "Cytonn", False, 0.0),
]


def upgrade() -> None:
    op.create_table(
        "tier_contract",
        sa.Column("contract_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("primary_channel", sa.Text(), nullable=False),
        sa.Column("secondary_channel", sa.Text(), nullable=True),
        sa.Column("max_words", sa.Integer(), nullable=False),
        sa.Column("sign_off", sa.Text(), nullable=False),
        sa.Column("human_approval", sa.Boolean(), nullable=False),
        sa.Column("review_sample_rate", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("contract_id"),
        sa.UniqueConstraint("version", "tier", name="uq_tier_contract_version_tier"),
    )
    op.create_index("ix_tier_contract_version", "tier_contract", ["version"], unique=False)

    seed = sa.table(
        "tier_contract",
        sa.column("version", sa.Integer),
        sa.column("tier", sa.Text),
        sa.column("display_name", sa.Text),
        sa.column("primary_channel", sa.Text),
        sa.column("secondary_channel", sa.Text),
        sa.column("max_words", sa.Integer),
        sa.column("sign_off", sa.Text),
        sa.column("human_approval", sa.Boolean),
        sa.column("review_sample_rate", sa.Float),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "tier": tier,
                "display_name": display_name,
                "primary_channel": primary_channel,
                "secondary_channel": secondary_channel,
                "max_words": max_words,
                "sign_off": sign_off,
                "human_approval": human_approval,
                "review_sample_rate": review_sample_rate,
                "valid_from": _VALID_FROM,
                "valid_to": None,
            }
            for (
                tier,
                display_name,
                primary_channel,
                secondary_channel,
                max_words,
                sign_off,
                human_approval,
                review_sample_rate,
            ) in _TIERS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_tier_contract_version", table_name="tier_contract")
    op.drop_table("tier_contract")
