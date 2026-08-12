"""business rules v4: hold_band rename, close v3's window

Revision ID: 209a9c997624
Revises: 8cd654fa2267
Create Date: 2026-08-11 22:50:00.000000

v3 is live in production (since fa894fc0413a), so its rows may never be
edited: three of its rules name "Parked briefly" in their match JSONB
(wrong_shelf, your_next_deposit, second_try), and that string no longer
exists once 8cd654fa2267 renames it to "Under 2m" everywhere else. The rename
ships as version 4, otherwise identical to version 3 rule for rule, and v3's
window closes the same day v4's opens so the two never overlap and neither
is ever both active and stale at once.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.rules.store import RuleSpec, save_version

# revision identifiers, used by Alembic.
revision: str = "209a9c997624"
down_revision: str | Sequence[str] | None = "8cd654fa2267"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V3_VALID_TO = date(2026, 8, 11)
_V4_VALID_FROM = date(2026, 8, 11)

# Same placeholders v3 shipped with: a rule naming T1-T4 defers tier and
# urgency to the client's own derived value (see rules/indicators.py), so
# these two never reach a generated message.
_URGENCY = "medium"
_TIER = "T3"

# Identical to v3's rule set, priority for priority, except the three rules
# that named "Parked briefly" now name "Under 2m".
_V4_RULES = [
    RuleSpec(
        name="not_a_goodbye",
        priority=10,
        match={"exit_reason": ["charge_settled"]},
        message_angle="not_a_goodbye",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="not_a_goodbye",
    ),
    RuleSpec(
        name="wrong_shelf",
        priority=20,
        match={"fund_type": ["high_yield"], "hold_band": ["Under 2m", "Under 6m"]},
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
        },
        message_angle="see_what_changed",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="see_what_changed",
    ),
    RuleSpec(
        name="the_long_hold",
        priority=40,
        match={"in_wave": ["true"], "hold_band": ["Stayed years"]},
        message_angle="the_long_hold",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="the_long_hold",
    ),
    RuleSpec(
        name="your_next_deposit",
        priority=50,
        match={"hold_band": ["Under 2m"], "purchase_depth": ["few", "capped"]},
        message_angle="your_next_deposit",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="your_next_deposit",
    ),
    RuleSpec(
        name="second_try",
        priority=60,
        match={"hold_band": ["Under 2m"], "purchase_depth": ["single"]},
        message_angle="second_try",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="second_try",
    ),
    RuleSpec(
        name="you_wound_down",
        priority=70,
        match={"staged_exit": ["true"]},
        message_angle="you_wound_down",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="you_wound_down",
    ),
    RuleSpec(
        name="you_were_scaling",
        priority=80,
        match={"trend_band": ["rising"]},
        message_angle="you_were_scaling",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="you_were_scaling",
    ),
    RuleSpec(
        name="you_were_fading",
        priority=90,
        match={"trend_band": ["falling"]},
        message_angle="you_were_fading",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="you_were_fading",
    ),
    RuleSpec(
        name="back_on_schedule",
        priority=100,
        match={"purchase_depth": ["capped"], "cadence_band": ["Tight"]},
        message_angle="back_on_schedule",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="back_on_schedule",
    ),
    RuleSpec(
        name="onboarding_retry",
        priority=110,
        match={"purchase_depth": ["single"]},
        message_angle="onboarding_retry",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="onboarding_retry",
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
    save_version(session, 4, _V4_RULES, valid_from=_V4_VALID_FROM)
    session.flush()
    op.execute(
        business_rules.update().where(business_rules.c.version == 3).values(valid_to=_V3_VALID_TO)
    )


def downgrade() -> None:
    op.execute(business_rules.update().where(business_rules.c.version == 3).values(valid_to=None))
    op.execute("DELETE FROM business_rules WHERE version = 4")
