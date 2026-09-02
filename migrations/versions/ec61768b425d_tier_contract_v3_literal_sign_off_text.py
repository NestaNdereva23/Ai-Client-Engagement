"""tier contract v3 with literal sign off text

Versions 1 and 2 stored sign_off as a category label ("named relationship
manager", "named person") rather than text a person would actually sign an
email with. The drafting prompt tells the model to use that value exactly,
but the same prompt also tells it never to invent a relationship manager's
name and never to write a placeholder for one, so the model reasonably
writes a natural sign off instead, such as "Best regards, Relationship
Manager". The outbound sign off guardrail then checks for the old label
verbatim and fails almost every draft, on every tier and every angle.

This version keeps every other field the same as version 2 and only
rewrites sign_off to literal text the model can plausibly write out in
full, with no invented personal name and no placeholder.

Revision ID: ec61768b425d
Revises: ea46ff789505
Create Date: 2026-09-01 17:30:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ec61768b425d"
down_revision: str | Sequence[str] | None = "ea46ff789505"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 3
_VALID_FROM = date(2026, 9, 1)

# tier, display_name, primary_channel, secondary_channel, max_words,
# sign_off, review_sample_rate, cohort_sample_rate
_TIERS = [
    ("T1", "Tier 1 top", "email", "call_brief", 120, "Your Relationship Manager", 1.0, 0.05),
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


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM tier_contract WHERE version = :v").bindparams(v=_VERSION))
