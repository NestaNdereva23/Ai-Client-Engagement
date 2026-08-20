"""generation_cost_config_version table

Revision ID: a7c4e91f2b6d
Revises: e8a3f26b9c41
Create Date: 2026-08-19 00:00:00.000000

The RAG-enabled per-generation cost rate, versioned exactly like
risk_config_version and template_policy_config_version so a cost estimate
given today stays explainable against the rate that was live when it ran.
Each supported model keeps its own version sequence.

Version 1 seeds one row per supported model. Claude Haiku 4.5's rate is the
"with RAG, single generation" figure measured in Generation Cost Estimate -
RAG versus No RAG.docx: $0.002877 / KES 0.37 per generation call, standard
(non-batch) API. Sonnet 5, Opus 5, and Fable 5 have no measured trace yet, so
their rates are derived from Anthropic's published per-token pricing applied
to the same average token profile the Haiku trace measured (1,930 input /
189 output tokens), converted at the same USD/KES rate the Haiku figure
implies (~128.61). They are placeholders, superseded by a new version once
each model has its own measured trace.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e91f2b6d"
down_revision: str | Sequence[str] | None = "e8a3f26b9c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_VALID_FROM = date(2026, 8, 19)


def upgrade() -> None:
    op.create_table(
        "generation_cost_config_version",
        sa.Column("config_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("cost_per_generation_usd", sa.Float(), nullable=False),
        sa.Column("cost_per_generation_kes", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("cost_per_generation_usd > 0", name="ck_gccv_usd_positive"),
        sa.CheckConstraint("cost_per_generation_kes > 0", name="ck_gccv_kes_positive"),
        sa.PrimaryKeyConstraint("config_id"),
    )
    op.create_index(
        "uq_gccv_model_version",
        "generation_cost_config_version",
        ["model", "version"],
        unique=True,
    )
    op.create_index(
        "ix_gccv_model_valid_from",
        "generation_cost_config_version",
        ["model", "valid_from"],
    )

    seed = sa.table(
        "generation_cost_config_version",
        sa.column("version", sa.Integer),
        sa.column("model", sa.Text),
        sa.column("cost_per_generation_usd", sa.Float),
        sa.column("cost_per_generation_kes", sa.Float),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "model": "claude-haiku-4-5-20251001",
                "cost_per_generation_usd": 0.002877,
                "cost_per_generation_kes": 0.37,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            },
            {
                "version": 1,
                "model": "claude-sonnet-5",
                "cost_per_generation_usd": 0.00575,
                "cost_per_generation_kes": 0.74,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            },
            {
                "version": 1,
                "model": "claude-opus-5",
                "cost_per_generation_usd": 0.014375,
                "cost_per_generation_kes": 1.85,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            },
            {
                "version": 1,
                "model": "claude-fable-5",
                "cost_per_generation_usd": 0.02875,
                "cost_per_generation_kes": 3.70,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_gccv_model_valid_from", table_name="generation_cost_config_version")
    op.drop_index("uq_gccv_model_version", table_name="generation_cost_config_version")
    op.drop_table("generation_cost_config_version")
