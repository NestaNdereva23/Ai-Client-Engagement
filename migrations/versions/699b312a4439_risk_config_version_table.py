"""risk config version table

Revision ID: 699b312a4439
Revises: dd2306473c5c
Create Date: 2026-08-11 11:57:42.704376

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "699b312a4439"
down_revision: str | Sequence[str] | None = "dd2306473c5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_VALID_FROM = date(2026, 8, 1)

# The notebook's own weights, summing to 100.
_V1_WEIGHTS = {
    "sig_drawdown": 30,
    "sig_dormant": 25,
    "sig_cadence_break": 20,
    "sig_shrinking": 15,
    "sig_fee_erosion": 7,
    "sig_never_repeated": 3,
}

# The notebook's own thresholds, all of them already KES: read directly off
# KES-denominated sale and balance figures, none converted from USD (the
# observed automatic fee postings sit around KES 50, so a KES 100 cap on
# SYSTEM_SALE_MAX and a KES 100 dust floor both come straight from that;
# MATERIAL_BALANCE is the same kind of figure, at KES 10,000).
#
# RISK_BAND_CUTOFFS is the one figure with no notebook constant behind it --
# the notebook bins risk_score with a literal pd.cut([-1, 0, 24, 49, 74,
# 100], ...) rather than a named variable. The four interior cutoffs are
# carried over unchanged; storing them here (rather than leaving them
# hard-coded) is what makes the band boundaries retunable like every other
# threshold, matching the implementation plan's own intent for this table.
_V1_THRESHOLDS = {
    "DORMANT_DAYS": 365,
    "DRAWDOWN_HEAVY": 0.50,
    "LAPSE_MULTIPLE": 3.0,
    "DECLINE_SLOPE": -0.10,
    "DUST_BALANCE": 100,
    "MATERIAL_BALANCE": 10_000,
    "FEE_RUNWAY_MONTHS": 12,
    "FEE_PER_MONTH": 50,
    "SYSTEM_SALE_MAX": 100,
    "RISK_BAND_CUTOFFS": [0, 24, 49, 74],
}


def upgrade() -> None:
    op.create_table(
        "risk_config_version",
        sa.Column("config_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("weights", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fa_call_capacity", sa.Integer(), nullable=False),
        sa.Column("at_risk_min", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("config_id"),
    )
    op.create_index(
        "ix_risk_config_version_version", "risk_config_version", ["version"], unique=True
    )

    seed = sa.table(
        "risk_config_version",
        sa.column("version", sa.Integer),
        sa.column("weights", postgresql.JSONB),
        sa.column("thresholds", postgresql.JSONB),
        sa.column("fa_call_capacity", sa.Integer),
        sa.column("at_risk_min", sa.Integer),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "weights": _V1_WEIGHTS,
                "thresholds": _V1_THRESHOLDS,
                "fa_call_capacity": 150,
                "at_risk_min": 25,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_risk_config_version_version", table_name="risk_config_version")
    op.drop_table("risk_config_version")
