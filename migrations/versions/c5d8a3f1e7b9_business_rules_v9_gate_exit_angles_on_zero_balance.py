"""business_rules v9: exit-framed angles require a zero current balance

Revision ID: c5d8a3f1e7b9
Revises: b3e7f2a9c4d6
Create Date: 2026-10-06 13:30:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.rules.store import RuleSpec, save_version

revision: str = "c5d8a3f1e7b9"
down_revision: str | Sequence[str] | None = "b3e7f2a9c4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V8_VALID_TO = date(2026, 10, 6)
_V9_VALID_FROM = date(2026, 10, 6)

_URGENCY = "medium"
_TIER = "one_time_withdrawers"

# Every angle below assumes the client already sold out, so each one now
# also requires a current balance of zero; a client still holding money
# falls through to former_high_value, grow_back, or the catch-all instead.
_V9_RULES = [
    RuleSpec(
        name="first_deposit",
        priority=1,
        match={"never_funded": ["true"]},
        message_angle="first_deposit_welcome",
        urgency="low",
        priority_tier="hot_leads",
        prompt_variant="first_deposit_welcome",
    ),
    RuleSpec(
        name="sitting_still",
        priority=5,
        match={"active_book_auto_checkin": ["true"]},
        message_angle="sitting_still",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="sitting_still",
    ),
    RuleSpec(
        name="not_a_goodbye",
        priority=10,
        match={"exit_reason": ["charge_settled"], "has_balance": ["false"]},
        message_angle="not_a_goodbye",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="not_a_goodbye",
    ),
    RuleSpec(
        name="wrong_shelf",
        priority=20,
        match={
            "fund_type": ["high_yield"],
            "hold_band": ["Under 2m", "Under 6m"],
            "has_balance": ["false"],
        },
        message_angle="wrong_shelf",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="wrong_shelf",
    ),
    RuleSpec(
        name="see_what_changed",
        priority=30,
        match={
            "in_wave": ["true"],
            "hold_band": ["Stayed years"],
            "has_depth": ["true"],
            "has_balance": ["false"],
        },
        message_angle="see_what_changed",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="see_what_changed",
    ),
    RuleSpec(
        name="the_long_hold",
        priority=40,
        match={"in_wave": ["true"], "hold_band": ["Stayed years"], "has_balance": ["false"]},
        message_angle="the_long_hold",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="the_long_hold",
    ),
    RuleSpec(
        name="your_next_deposit",
        priority=50,
        match={
            "hold_band": ["Under 2m"],
            "purchase_depth": ["few", "capped"],
            "has_balance": ["false"],
        },
        message_angle="your_next_deposit",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="your_next_deposit",
    ),
    RuleSpec(
        name="second_try",
        priority=60,
        match={"hold_band": ["Under 2m"], "purchase_depth": ["single"], "has_balance": ["false"]},
        message_angle="second_try",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="second_try",
    ),
    RuleSpec(
        name="you_wound_down",
        priority=70,
        match={"staged_exit": ["true"], "has_balance": ["false"]},
        message_angle="you_wound_down",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="you_wound_down",
    ),
    RuleSpec(
        name="you_were_scaling",
        priority=80,
        match={"trend_band": ["rising"], "has_balance": ["false"]},
        message_angle="you_were_scaling",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="you_were_scaling",
    ),
    RuleSpec(
        name="you_were_fading",
        priority=90,
        match={"trend_band": ["falling"], "has_balance": ["false"]},
        message_angle="you_were_fading",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="you_were_fading",
    ),
    RuleSpec(
        name="back_on_schedule",
        priority=100,
        match={
            "purchase_depth": ["capped"],
            "cadence_band": ["Tight"],
            "has_balance": ["false"],
        },
        message_angle="back_on_schedule",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="back_on_schedule",
    ),
    RuleSpec(
        name="onboarding_retry",
        priority=110,
        match={"purchase_depth": ["single"], "has_balance": ["false"]},
        message_angle="onboarding_retry",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="onboarding_retry",
    ),
    RuleSpec(
        name="former_high_value",
        priority=115,
        match={"high_value": ["true"]},
        message_angle="former_high_value",
        urgency=_URGENCY,
        priority_tier="low_depositors",
        prompt_variant="former_high_value",
    ),
    RuleSpec(
        name="grow_back",
        priority=117,
        match={"balance_band": ["worth_saving"]},
        message_angle="grow_back",
        urgency=_URGENCY,
        priority_tier="low_depositors",
        prompt_variant="grow_back",
    ),
    RuleSpec(
        name="pick_up_again",
        priority=120,
        match={},
        message_angle="pick_up_again",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="pick_up_again",
    ),
]

business_rules = sa.table(
    "business_rules",
    sa.column("version", sa.Integer),
    sa.column("valid_to", sa.Date),
)


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_version(session, 9, _V9_RULES, valid_from=_V9_VALID_FROM, validate=False)
    session.flush()
    op.execute(
        business_rules.update().where(business_rules.c.version == 8).values(valid_to=_V8_VALID_TO)
    )


def downgrade() -> None:
    op.execute(business_rules.update().where(business_rules.c.version == 8).values(valid_to=None))
    op.execute("DELETE FROM business_rules WHERE version = 9")
