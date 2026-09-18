"""personalization policy v2: month_the_account_empties for the dormant
fee pressure angle

fee_warning was split into fee_pressure_warning_dormant and
fee_pressure_encourage_active (message_angle_catalog v6), but the fact
eligibility rule that lets an angle name the month a balance runs out was
never carried over to either new angle name. graph.load_client_context now
fetches month_the_account_empties for every angle and leaves whether it may
actually be cited to this table, so without a rule here the fact reaches no
one.

fee_pressure_warning_dormant's own brief says naming that month is exactly
its job, so it gets the same conditional rule fee_warning already had.
fee_pressure_encourage_active's brief never mentions the month, and its
never clause bans implying the account is running out, so it gets no rule
and stays prohibited by the same default every other unlisted field falls
back to.

Every rule already in force is carried over unchanged, so this version only
adds one row.

Revision ID: e3a9f6c2b7d4
Revises: d1a6b9c4e7f2
Create Date: 2026-09-16 23:15:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

from alembic import op
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.prompt_config import (
    ActiveConfiguration,
    FactEligibilityRule,
    PersonalizationPolicy,
)

revision: str = "e3a9f6c2b7d4"
down_revision: str | Sequence[str] | None = "d1a6b9c4e7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_VERSION = 1
_VERSION = 2
_VALID_FROM = date(2026, 9, 16)
_NEW_ANGLE = "fee_pressure_warning_dormant"
_FACT_FIELD = "month_the_account_empties"


def upgrade() -> None:
    session = Session(bind=op.get_bind())

    carried_over = session.scalars(
        select(FactEligibilityRule).where(FactEligibilityRule.policy_version == _PREVIOUS_VERSION)
    ).all()

    session.add(
        PersonalizationPolicy(
            version=_VERSION,
            status="published",
            valid_from=_VALID_FROM,
            valid_to=None,
            published_at=datetime.now(UTC),
        )
    )
    for rule in carried_over:
        session.add(
            FactEligibilityRule(
                policy_version=_VERSION,
                angle=rule.angle,
                fact_field=rule.fact_field,
                exposure_mode=rule.exposure_mode,
                condition=rule.condition,
            )
        )
    session.add(
        FactEligibilityRule(
            policy_version=_VERSION,
            angle=_NEW_ANGLE,
            fact_field=_FACT_FIELD,
            exposure_mode="conditional",
            condition=f"{_FACT_FIELD} is not None",
        )
    )

    previous_policy = session.scalar(
        select(PersonalizationPolicy).where(PersonalizationPolicy.version == _PREVIOUS_VERSION)
    )
    if previous_policy is not None:
        previous_policy.valid_to = _VALID_FROM

    active_configuration = session.scalar(
        select(ActiveConfiguration).where(
            ActiveConfiguration.component_type == "personalization_policy",
            ActiveConfiguration.component_key == "default",
        )
    )
    if active_configuration is not None:
        active_configuration.active_version = _VERSION

    session.flush()


def downgrade() -> None:
    session = Session(bind=op.get_bind())

    previous_policy = session.scalar(
        select(PersonalizationPolicy).where(PersonalizationPolicy.version == _PREVIOUS_VERSION)
    )
    if previous_policy is not None:
        previous_policy.valid_to = None

    active_configuration = session.scalar(
        select(ActiveConfiguration).where(
            ActiveConfiguration.component_type == "personalization_policy",
            ActiveConfiguration.component_key == "default",
        )
    )
    if active_configuration is not None:
        active_configuration.active_version = _PREVIOUS_VERSION

    session.execute(
        FactEligibilityRule.__table__.delete().where(FactEligibilityRule.policy_version == _VERSION)
    )
    session.execute(
        PersonalizationPolicy.__table__.delete().where(PersonalizationPolicy.version == _VERSION)
    )
    session.flush()
