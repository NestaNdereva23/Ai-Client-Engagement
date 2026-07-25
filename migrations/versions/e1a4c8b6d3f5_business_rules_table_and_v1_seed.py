"""business_rules table and v1 rule seed

Revision ID: e1a4c8b6d3f5
Revises: d7b3e9f4a1c2
Create Date: 2026-07-25 09:00:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e1a4c8b6d3f5"
down_revision: str | Sequence[str] | None = "d7b3e9f4a1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The v1 rule set: ordered most specific first, resolved first-match-wins by the
# engine. archetype drives the angle, value tier lifts the priority, and a final
# wildcard keeps every client covered.
_V1_VALID_FROM = date(2026, 1, 1)
_V1_RULES = [
    (
        "frequent_high_value",
        10,
        {"archetype": ["Frequent (5+, censored)"], "value_tier": ["Top", "High"]},
        "winback_habit",
        "high",
        "P1",
        "habit_premium",
    ),
    (
        "frequent_default",
        20,
        {"archetype": ["Frequent (5+, censored)"]},
        "winback_habit",
        "medium",
        "P2",
        "habit_standard",
    ),
    (
        "occasional_high_value",
        30,
        {"archetype": ["Occasional (2-4)"], "value_tier": ["Top", "High"]},
        "winback_habit",
        "medium",
        "P2",
        "habit_standard",
    ),
    (
        "occasional_default",
        40,
        {"archetype": ["Occasional (2-4)"]},
        "winback_flexible",
        "medium",
        "P3",
        "flexible_standard",
    ),
    (
        "one_and_done_high_value",
        50,
        {"archetype": ["One-and-done"], "value_tier": ["Top", "High"]},
        "winback_flexible",
        "medium",
        "P2",
        "flexible_premium",
    ),
    (
        "one_and_done_default",
        60,
        {"archetype": ["One-and-done"]},
        "winback_flexible",
        "low",
        "P3",
        "flexible_standard",
    ),
    (
        "none_observed",
        70,
        {"archetype": ["None observed"]},
        "winback_flexible",
        "low",
        "P3",
        "flexible_minimal",
    ),
    ("catch_all", 80, {}, "winback_flexible", "low", "P3", "flexible_standard"),
]


def upgrade() -> None:
    op.create_table(
        "business_rules",
        sa.Column("rule_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("match", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message_angle", sa.Text(), nullable=False),
        sa.Column("urgency", sa.Text(), nullable=False),
        sa.Column("priority_tier", sa.Text(), nullable=False),
        sa.Column("prompt_variant", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("rule_id"),
        sa.UniqueConstraint("version", "priority", name="uq_business_rules_version_priority"),
    )
    op.create_index("ix_business_rules_version", "business_rules", ["version"])

    seed = sa.table(
        "business_rules",
        sa.column("version", sa.Integer),
        sa.column("priority", sa.Integer),
        sa.column("name", sa.Text),
        sa.column("match", postgresql.JSONB),
        sa.column("message_angle", sa.Text),
        sa.column("urgency", sa.Text),
        sa.column("priority_tier", sa.Text),
        sa.column("prompt_variant", sa.Text),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "priority": priority,
                "name": name,
                "match": match,
                "message_angle": angle,
                "urgency": urgency,
                "priority_tier": tier,
                "prompt_variant": variant,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            }
            for (name, priority, match, angle, urgency, tier, variant) in _V1_RULES
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_business_rules_version", table_name="business_rules")
    op.drop_table("business_rules")
