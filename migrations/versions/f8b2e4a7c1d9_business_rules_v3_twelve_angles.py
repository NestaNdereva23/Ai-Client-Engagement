from collections.abc import Sequence
from datetime import date

from alembic import op
from sqlalchemy.orm import Session

from app.rules.store import RuleSpec, save_version

# revision identifiers, used by Alembic.
revision: str = "f8b2e4a7c1d9"
down_revision: str | Sequence[str] | None = "e6a3c8d5f2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Provisional. Not live yet; see the module docstring.
_V3_VALID_FROM = date(2026, 12, 1)

# Placeholder until V4 wires the derived tier through; see the module docstring.
_URGENCY = "medium"
_TIER = "T3"

# In the order the router applies them: facts that constrain what may be said
# sit above behavioural patterns, specific patterns above general ones, and the
# last rule is the deliberate general case that guarantees full coverage.
_V3_RULES = [
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
        match={"fund_type": ["high_yield"], "hold_band": ["Parked briefly", "Under 6m"]},
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
        match={"hold_band": ["Parked briefly"], "purchase_depth": ["few", "capped"]},
        message_angle="your_next_deposit",
        urgency=_URGENCY,
        priority_tier=_TIER,
        prompt_variant="your_next_deposit",
    ),
    RuleSpec(
        name="second_try",
        priority=60,
        match={"hold_band": ["Parked briefly"], "purchase_depth": ["single"]},
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


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_version(session, 3, _V3_RULES, valid_from=_V3_VALID_FROM)
    session.flush()


def downgrade() -> None:
    op.execute("DELETE FROM business_rules WHERE version = 3")
