"""agent_action_catalog v2: a ninth action for a queue that got more urgent
with nobody in touch

Revision ID: 6f3da7fbaf89
Revises: f198420ccf56
Create Date: 2026-09-07 12:00:00.000000

The watch list now has a group for a client whose place in the risk queue
got more urgent overnight, for any one of several reasons, with nobody
reaching out since. None of the eight starting actions describe that
honestly: each of the others is written for one specific cause (deposits
shrinking, a balance running out, a missed call), and this group can be any
of them at once. It reuses the sitting_still message angle rather than a new
one, since that angle already refuses to state a specific figure, band or
date and was written for this kind of active-book check-in, not the dormant
win back router.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "6f3da7fbaf89"
down_revision: str | Sequence[str] | None = "f198420ccf56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 9, 7)

_ACTIONS = [
    dict(
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
    dict(
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
    dict(
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
    dict(
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
    dict(
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
    dict(
        action_code="send_learning_note",
        title="Send a short learning note",
        who="Anyone in a quiet period where there is nothing to sell",
        evidence_required="No other action applies and the client is not held or suppressed",
        message_angle="sitting_still",
        channel="email",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
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
    dict(
        action_code="do_nothing",
        title="Do nothing, and record why",
        who="Anyone the gates rule out",
        evidence_required="A named gate that removed the group, with the count it removed",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
        action_code="check_in_rising_risk",
        title="Check in before it gets worse",
        who=(
            "Someone whose place in the risk queue got more urgent overnight, for "
            "whatever reason, and who nobody has reached yet"
        ),
        evidence_required=(
            "The client's queue position moved to a more urgent route since the run "
            "before, with nothing logged against them since"
        ),
        message_angle="sitting_still",
        channel="email",
        content_mix="balanced",
        default_permission="suggest_only",
        money_ceiling_kes=1000000.0,
    ),
]


_COLUMNS = (
    "version",
    "action_code",
    "title",
    "who",
    "evidence_required",
    "message_angle",
    "channel",
    "content_mix",
    "default_permission",
    "money_ceiling_kes",
    "paused",
    "valid_from",
    "valid_to",
)

# The columns as they stood when this version shipped. Written out here so a
# later change to the table never rewrites what this migration inserts.
_CATALOG_TABLE = sa.table("agent_action_catalog", *(sa.column(name) for name in _COLUMNS))


def _rows() -> list[dict]:
    """Every action of version 2, with the same value in every column."""
    blank = {name: None for name in _COLUMNS}
    return [
        {**blank, **action, "version": 2, "paused": False, "valid_from": _VALID_FROM}
        for action in _ACTIONS
    ]


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE agent_action_catalog SET valid_to = :valid_from "
            "WHERE version < 2 AND valid_to IS NULL"
        ).bindparams(valid_from=_VALID_FROM)
    )
    op.bulk_insert(_CATALOG_TABLE, _rows())


def downgrade() -> None:
    op.execute("DELETE FROM agent_action_catalog WHERE version = 2")
    op.execute("UPDATE agent_action_catalog SET valid_to = NULL WHERE version = 1")
