"""tier contract v5: withdrawal cluster tiers

Revision ID: 8a329564c6d6
Revises: ee3c3e393606
Create Date: 2026-09-29 21:10:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "8a329564c6d6"
down_revision: str | Sequence[str] | None = "ee3c3e393606"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 5
_VALID_FROM = date(2026, 9, 29)
_EARLIER_VERSIONS = (1, 2, 3, 4)
_SIGN_OFF = "Cytonn Asset Managers"

_TIERS = [
    ("gradual_withdrawers", "Gradual withdrawer", 130),
    ("frequent_withdrawers", "Frequent withdrawer", 130),
    ("one_time_withdrawers", "One-time withdrawer", 120),
    ("low_depositors", "Low depositor", 90),
    ("hot_leads", "Hot lead", 90),
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
                "primary_channel": "email",
                "secondary_channel": "sms",
                "max_words": max_words,
                "sign_off": _SIGN_OFF,
                "human_approval": True,
                "review_sample_rate": 0.01,
                "cohort_sample_rate": 0.01,
                "valid_from": _VALID_FROM,
                "valid_to": None,
            }
            for (tier, display_name, max_words) in _TIERS
        ],
    )
    op.execute(
        contract.update()
        .where(contract.c.version.in_(_EARLIER_VERSIONS))
        .values(valid_to=_VALID_FROM)
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE tier_contract SET valid_to = NULL WHERE version IN (1, 2, 3, 4)"))
    op.execute(sa.text("DELETE FROM tier_contract WHERE version = :v").bindparams(v=_VERSION))
