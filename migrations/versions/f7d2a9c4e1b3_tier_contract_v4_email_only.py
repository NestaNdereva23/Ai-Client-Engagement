"""tier contract v4 with email only on every tier

The dormant win back makes no outbound calls. Every client, in every tier, is
reached by email, and a call only happens after a client replies. Version 3
gave Tier 1 a call brief as a second channel, which prepared a relationship
manager to ring every Tier 1 client. This version drops that second channel
and keeps every other field the same as version 3.

Versions 1 to 3 are closed on the day this one starts. Version 1 was seeded
with a later start date than the versions that replaced it, so without an
end date it would take over again on that date and bring the call brief back.

Revision ID: f7d2a9c4e1b3
Revises: e4c7a1b9d2f8
Create Date: 2026-09-11 10:00:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7d2a9c4e1b3"
down_revision: str | Sequence[str] | None = "e4c7a1b9d2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 4
_VALID_FROM = date(2026, 9, 11)
_EARLIER_VERSIONS = (1, 2, 3)

# tier, display_name, primary_channel, secondary_channel, max_words,
# sign_off, review_sample_rate, cohort_sample_rate
_TIERS = [
    ("T1", "Tier 1 top", "email", None, 120, "Your Relationship Manager", 1.0, 0.05),
    ("T2", "Tier 2 high", "email", None, 140, "Your Cytonn Contact", 0.1, 0.03),
    ("T3", "Tier 3 medium", "email", None, 110, "Client Services", 0.02, 0.02),
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
    op.execute(
        contract.update()
        .where(contract.c.version.in_(_EARLIER_VERSIONS))
        .values(valid_to=_VALID_FROM)
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE tier_contract SET valid_to = NULL WHERE version IN (1, 2, 3)"))
    op.execute(sa.text("DELETE FROM tier_contract WHERE version = :v").bindparams(v=_VERSION))
