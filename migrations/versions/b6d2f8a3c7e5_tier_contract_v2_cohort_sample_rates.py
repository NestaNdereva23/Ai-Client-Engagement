"""tier contract v2 with cohort sample rates

Version 1 is dated 1 December 2026, so no contract is in force before then
and cohort sampling has nothing to read: every message ends up a sample.
Version 2 puts the same four tiers in force from today, carrying the new
cohort sample rates.

human_approval is true for all four tiers here, which is what the system
already does today with no contract in force at all. This version is only
meant to switch cohort sampling on; it deliberately leaves the separate
question of whether a template instance gets its own review exactly where
it stands. Version 1's own flags take over on 1 December as always planned,
so its rows are backfilled with the same rates to keep cohort sampling
working across that handover.

Revision ID: b6d2f8a3c7e5
Revises: f1a9c3e7b2d4
Create Date: 2026-08-19 18:00:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d2f8a3c7e5"
down_revision: str | Sequence[str] | None = "f1a9c3e7b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 2
_VALID_FROM = date(2026, 8, 19)

# tier, display_name, primary_channel, secondary_channel, max_words,
# sign_off, review_sample_rate, cohort_sample_rate
_TIERS = [
    ("T1", "Tier 1 top", "email", "call_brief", 120, "named relationship manager", 1.0, 0.05),
    ("T2", "Tier 2 high", "email", None, 140, "named person", 0.1, 0.03),
    ("T3", "Tier 3 medium", "email", None, 110, "client services", 0.02, 0.02),
    ("T4", "Tier 4 low", "email", "batch", 60, "Cytonn", 0.0, 0.01),
]


def upgrade() -> None:
    contract = sa.table(
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
        sa.column("cohort_sample_rate", sa.Float),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        contract,
        [
            {
                "version": _VERSION,
                "tier": tier,
                "display_name": display_name,
                "primary_channel": primary_channel,
                "secondary_channel": secondary_channel,
                "max_words": max_words,
                "sign_off": sign_off,
                "human_approval": True,
                "review_sample_rate": review_sample_rate,
                "cohort_sample_rate": cohort_sample_rate,
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
                review_sample_rate,
                cohort_sample_rate,
            ) in _TIERS
        ],
    )

    for row in _TIERS:
        tier, cohort_sample_rate = row[0], row[7]
        op.execute(
            sa.text(
                "UPDATE tier_contract SET cohort_sample_rate = :rate "
                "WHERE version = 1 AND tier = :tier"
            ).bindparams(rate=cohort_sample_rate, tier=tier)
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM tier_contract WHERE version = :v").bindparams(v=_VERSION))
    op.execute(sa.text("UPDATE tier_contract SET cohort_sample_rate = NULL WHERE version = 1"))
