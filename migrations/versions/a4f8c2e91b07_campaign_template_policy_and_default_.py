"""campaign_template_policy and default config version

Revision ID: a4f8c2e91b07
Revises: 209a9c997624
Create Date: 2026-08-11 23:10:00.000000

Two tables for the template generation limit: campaign_template_policy holds
the one override a campaign manager may set per campaign; a campaign with no
row here inherits template_policy_config_version, versioned exactly like
risk_config_version so an old drafting call stays explainable against the
default that was live when it ran. Version 1 seeds no limit at all (both
fields null), matching today's unlimited behaviour -- this migration only
makes a limit possible, it does not impose one.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4f8c2e91b07"
down_revision: str | Sequence[str] | None = "209a9c997624"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_VALID_FROM = date(2026, 8, 11)


def upgrade() -> None:
    op.create_table(
        "campaign_template_policy",
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("max_templates", sa.Integer(), nullable=True),
        sa.Column("max_templates_pct", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "max_templates IS NULL OR max_templates > 0", name="ck_ctp_max_positive"
        ),
        sa.CheckConstraint(
            "max_templates_pct IS NULL OR (max_templates_pct >= 1 AND max_templates_pct <= 100)",
            name="ck_ctp_pct_range",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.campaign_id"]),
        sa.PrimaryKeyConstraint("campaign_id"),
    )

    op.create_table(
        "template_policy_config_version",
        sa.Column("config_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("default_max_templates", sa.Integer(), nullable=True),
        sa.Column("default_max_templates_pct", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "default_max_templates IS NULL OR default_max_templates > 0",
            name="ck_tpcv_max_positive",
        ),
        sa.CheckConstraint(
            "default_max_templates_pct IS NULL "
            "OR (default_max_templates_pct >= 1 AND default_max_templates_pct <= 100)",
            name="ck_tpcv_pct_range",
        ),
        sa.PrimaryKeyConstraint("config_id"),
    )
    op.create_index(
        "ix_template_policy_config_version_version",
        "template_policy_config_version",
        ["version"],
        unique=True,
    )

    seed = sa.table(
        "template_policy_config_version",
        sa.column("version", sa.Integer),
        sa.column("default_max_templates", sa.Integer),
        sa.column("default_max_templates_pct", sa.Integer),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "default_max_templates": None,
                "default_max_templates_pct": None,
                "valid_from": _V1_VALID_FROM,
                "valid_to": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_template_policy_config_version_version", table_name="template_policy_config_version"
    )
    op.drop_table("template_policy_config_version")
    op.drop_table("campaign_template_policy")
