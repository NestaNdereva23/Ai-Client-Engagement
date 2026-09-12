"""fact eligibility rule and personalization policy v1

Adds fact_eligibility_rule, the table that says which angle may use which
ModelFactBlock field and how (direct, placeholder, conditional, or
prohibited). Seeds it with Personalization Policy v1, reproducing exactly
what email_agent.PLACEHOLDER_FACT_FIELDS and graph._angle_only_facts already
do today: the six placeholder-filled fields, fund_name stated directly, and
month_the_account_empties for the fee_warning angle only. Every other field
is left unmentioned, which resolve_fact_eligibility treats as prohibited.

Also marks personalization_policy version 1 published and points
active_configuration at it, so the new table starts in agreement with what
is actually live.

Revision ID: a2e5c9d47f1b
Revises: 6c87e3ac8878
Create Date: 2026-09-11 15:00:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a2e5c9d47f1b"
down_revision: str | Sequence[str] | None = "6c87e3ac8878"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_VERSION = 1
_VALID_FROM = date(2026, 9, 11)

_PLACEHOLDER_FIELDS = (
    "typical_contribution_kes",
    "largest_contribution_kes",
    "years_since_exit",
    "days_held_after_last_topup",
    "month_they_left",
    "invested_every_n_days",
)


def upgrade() -> None:
    op.create_table(
        "fact_eligibility_rule",
        sa.Column("rule_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=True),
        sa.Column("fact_field", sa.Text(), nullable=False),
        sa.Column("exposure_mode", sa.Text(), nullable=False),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("rule_id"),
        sa.UniqueConstraint(
            "policy_version", "angle", "fact_field", name="uq_fact_eligibility_rule_scope"
        ),
        sa.CheckConstraint(
            "exposure_mode IN ('direct', 'placeholder', 'conditional', 'prohibited')",
            name="ck_fact_eligibility_rule_exposure_mode",
        ),
    )
    op.create_index(
        "ix_fact_eligibility_rule_policy_version", "fact_eligibility_rule", ["policy_version"]
    )

    rule = sa.table(
        "fact_eligibility_rule",
        sa.column("policy_version", sa.Integer),
        sa.column("angle", sa.Text),
        sa.column("fact_field", sa.Text),
        sa.column("exposure_mode", sa.Text),
        sa.column("condition", sa.Text),
    )
    op.bulk_insert(
        rule,
        [
            {
                "policy_version": _POLICY_VERSION,
                "angle": None,
                "fact_field": field,
                "exposure_mode": "placeholder",
                "condition": None,
            }
            for field in _PLACEHOLDER_FIELDS
        ]
        + [
            {
                "policy_version": _POLICY_VERSION,
                "angle": None,
                "fact_field": "fund_name",
                "exposure_mode": "direct",
                "condition": None,
            },
            {
                "policy_version": _POLICY_VERSION,
                "angle": "fee_warning",
                "fact_field": "month_the_account_empties",
                "exposure_mode": "conditional",
                "condition": "month_the_account_empties is not None",
            },
        ],
    )

    policy = sa.table(
        "personalization_policy",
        sa.column("version", sa.Integer),
        sa.column("status", sa.Text),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        policy,
        [
            {
                "version": _POLICY_VERSION,
                "status": "published",
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "published_at": datetime.now(UTC),
            }
        ],
    )

    active_configuration = sa.table(
        "active_configuration",
        sa.column("component_type", sa.Text),
        sa.column("component_key", sa.Text),
        sa.column("active_version", sa.Integer),
    )
    op.bulk_insert(
        active_configuration,
        [
            {
                "component_type": "personalization_policy",
                "component_key": "default",
                "active_version": _POLICY_VERSION,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM active_configuration "
            "WHERE component_type = 'personalization_policy' AND component_key = 'default'"
        )
    )
    op.execute(
        sa.text("DELETE FROM personalization_policy WHERE version = :v").bindparams(
            v=_POLICY_VERSION
        )
    )
    op.drop_index("ix_fact_eligibility_rule_policy_version", table_name="fact_eligibility_rule")
    op.drop_table("fact_eligibility_rule")
