"""business_rules v2 seed with censoring-aware softening

Revision ID: a3f7e2c9b8d1
Revises: f2c9d5a7b4e8
Create Date: 2026-07-25 13:00:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a3f7e2c9b8d1"
down_revision: str | Sequence[str] | None = "f2c9d5a7b4e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# v2 supersedes v1 from this date. It adds soft variants that avoid asserting
# exact counts or totals a censored window cannot support: the frequent (always
# censored) rules go soft, and a censored occasional or one-and-done client is
# caught before its value-based rule and softened.
_V2_VALID_FROM = date(2026, 8, 1)
_V2_RULES = [
    (
        "frequent_high_value",
        10,
        {"archetype": ["Frequent (5+, censored)"], "value_tier": ["Top", "High"]},
        "winback_habit",
        "high",
        "P1",
        "habit_premium_soft",
    ),
    (
        "frequent_default",
        20,
        {"archetype": ["Frequent (5+, censored)"]},
        "winback_habit",
        "medium",
        "P2",
        "habit_standard_soft",
    ),
    (
        "occasional_censored",
        30,
        {"archetype": ["Occasional (2-4)"], "history_censored": ["true"]},
        "winback_flexible",
        "medium",
        "P3",
        "flexible_soft",
    ),
    (
        "occasional_high_value",
        35,
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
        "one_and_done_censored",
        50,
        {"archetype": ["One-and-done"], "history_censored": ["true"]},
        "winback_flexible",
        "low",
        "P3",
        "flexible_soft",
    ),
    (
        "one_and_done_high_value",
        55,
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
                "version": 2,
                "priority": priority,
                "name": name,
                "match": match,
                "message_angle": angle,
                "urgency": urgency,
                "priority_tier": tier,
                "prompt_variant": variant,
                "valid_from": _V2_VALID_FROM,
                "valid_to": None,
            }
            for (name, priority, match, angle, urgency, tier, variant) in _V2_RULES
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM business_rules WHERE version = 2")
