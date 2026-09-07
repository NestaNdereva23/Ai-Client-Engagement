"""agent_action_catalog, with the eight starting actions

Revision ID: b3d7f1a9c5e2
Revises: a7c2e5f9b1d4
Create Date: 2026-09-07 09:00:00.000000

The agent proposes an action for a group of clients, and a person has to be
able to read that decision back long afterwards. The wording that produced it
therefore lives in the database as a numbered version with its own validity
window, never edited in place, the same way the message angle catalogue does.
Everything starts at suggest only: nothing runs without a person saying yes.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import ActionSpec, save_action_catalog_version

# revision identifiers, used by Alembic.
revision: str = "b3d7f1a9c5e2"
down_revision: str | Sequence[str] | None = "a7c2e5f9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 9, 7)

_ACTIONS = [
    ActionSpec(
        action_code="welcome_and_top_up",
        title="Welcome and ask for a small top up",
        who=(
            "Someone who signed up recently, has paid in once, and holds a "
            "balance small enough that the monthly fee matters to them"
        ),
        evidence_required=(
            "A first deposit date inside the recent window, exactly one "
            "deposit on the fund, and a balance above zero"
        ),
        message_angle="onboarding_retry",
        channel="email",
        content_mix="mostly_learning",
        default_permission="suggest_only",
        money_ceiling_kes=500000.0,
    ),
    ActionSpec(
        action_code="fee_warning",
        title="Tell them the fee will empty the account",
        who="Someone whose balance runs out within a few months at the current fee",
        evidence_required=("Months until empty below the threshold, and a balance above zero"),
        message_angle="sitting_still",
        channel="email",
        content_mix="balanced",
        default_permission="suggest_only",
        money_ceiling_kes=500000.0,
    ),
    ActionSpec(
        action_code="start_win_back",
        title="Start the win back sequence",
        who="Someone with a very small balance who has paid nothing in for a long time",
        evidence_required=(
            "A balance under the small balance threshold and the quiet deposits flag"
        ),
        message_angle="not_a_goodbye",
        channel="email",
        content_mix="mostly_ask",
        default_permission="suggest_only",
        money_ceiling_kes=1000000.0,
    ),
    ActionSpec(
        action_code="ask_what_changed",
        title="Ask whether something changed",
        who="Someone whose deposits are getting smaller over time",
        evidence_required="The shrinking deposits flag",
        message_angle="see_what_changed",
        channel="email",
        content_mix="mostly_learning",
        default_permission="suggest_only",
        money_ceiling_kes=1000000.0,
    ),
    ActionSpec(
        action_code="suggest_second_fund",
        title="Suggest a second fund that fits",
        who="Someone healthy who holds one fund only",
        evidence_required=("A risk band of none or low, and exactly one fund held by that client"),
        message_angle="wrong_shelf",
        channel="email",
        content_mix="balanced",
        default_permission="suggest_only",
        money_ceiling_kes=2000000.0,
    ),
    ActionSpec(
        action_code="send_learning_note",
        title="Send a short learning note",
        who="Anyone in a quiet period where there is nothing to sell",
        evidence_required="No other action applies and the client is not held or suppressed",
        message_angle="sitting_still",
        channel="email",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    ActionSpec(
        action_code="follow_up_when_no_one_called",
        title="Follow up when nobody called",
        who="Someone on the call list for two days with nothing logged against them",
        evidence_required=(
            "An open call task older than two days with no interaction recorded since"
        ),
        message_angle="sitting_still",
        channel="email",
        content_mix="balanced",
        default_permission="suggest_only",
        money_ceiling_kes=1000000.0,
    ),
    ActionSpec(
        action_code="do_nothing",
        title="Do nothing, and record why",
        who="Anyone the gates rule out",
        evidence_required="A named gate that removed the group, with the count it removed",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
]


def upgrade() -> None:
    op.create_table(
        "agent_action_catalog",
        sa.Column("catalog_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action_code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("who", sa.Text(), nullable=False),
        sa.Column("evidence_required", sa.Text(), nullable=False),
        sa.Column("message_angle", sa.Text(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("content_mix", sa.Text(), nullable=False),
        sa.Column("default_permission", sa.Text(), nullable=False),
        sa.Column("money_ceiling_kes", sa.Float(), nullable=True),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_mix IN ('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
            name="ck_agent_action_catalog_content_mix",
        ),
        sa.CheckConstraint(
            "default_permission IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_action_catalog_default_permission",
        ),
        sa.CheckConstraint(
            "money_ceiling_kes IS NULL OR money_ceiling_kes > 0",
            name="ck_agent_action_catalog_money_ceiling_positive",
        ),
        sa.PrimaryKeyConstraint("catalog_id"),
        sa.UniqueConstraint(
            "version", "action_code", name="uq_agent_action_catalog_version_action_code"
        ),
    )
    op.create_index(op.f("ix_agent_action_catalog_version"), "agent_action_catalog", ["version"])

    session = Session(bind=op.get_bind())
    save_action_catalog_version(session, 1, _ACTIONS, valid_from=_VALID_FROM)
    session.flush()


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_action_catalog_version"), table_name="agent_action_catalog")
    op.drop_table("agent_action_catalog")
