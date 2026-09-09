"""agent_action_catalog v3: the responses that are not an email

Revision ID: f3a90d27c614
Revises: e7b41c93a05d
Create Date: 2026-09-09 10:00:00.000000

Version 2 could only answer a finding by sending something. Most of what the
business actually does about a client is not an email: an adviser picks up a
task, somebody calls, the group goes into a campaign, the client is treated
differently from now on, or the whole thing goes to a person to decide. Six
of those are added here, and every action now says which sort it is. Nothing
gains any freedom: everything still starts at suggest only, and none of the
new actions sends a message, so none of them carries an angle or a channel.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "f3a90d27c614"
down_revision: str | Sequence[str] | None = "e7b41c93a05d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 9, 9)

_VERSION = 3

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
        response_kind="automated_email",
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
        evidence_required="Months until empty below the threshold, and a balance above zero",
        response_kind="automated_email",
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
        evidence_required="A balance under the small balance threshold and the quiet deposits flag",
        response_kind="automated_email",
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
        response_kind="automated_email",
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
        evidence_required="A risk band of none or low, and exactly one fund held by that client",
        response_kind="automated_email",
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
        response_kind="product_teaching",
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
        response_kind="automated_email",
        message_angle="sitting_still",
        channel="email",
        content_mix="balanced",
        default_permission="suggest_only",
        money_ceiling_kes=1000000.0,
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
        response_kind="automated_email",
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
        response_kind="monitor_only",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
        action_code="watch_for_now",
        title="Watch this group and do nothing yet",
        who="A group worth keeping an eye on where nothing needs saying today",
        evidence_required="A finding a person accepted, with no change that needs answering yet",
        response_kind="monitor_only",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
        action_code="give_advisor_a_task",
        title="Put a task on an adviser's list",
        who="A group a person should look at one client at a time",
        evidence_required="A finding a person accepted, naming the group the task covers",
        response_kind="advisor_task",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
        action_code="ask_for_a_call",
        title="Ask someone to call them",
        who="A group where a conversation will do more than a message",
        evidence_required="A finding a person accepted, and no call logged inside the wait window",
        response_kind="phone_call",
        content_mix="learning_only",
        default_permission="suggest_only",
        money_ceiling_kes=2000000.0,
    ),
    dict(
        action_code="add_to_campaign",
        title="Put the group into a campaign",
        who="A group large enough to be worth a run of messages rather than one",
        evidence_required="A finding a person accepted, with a group the campaign rules accept",
        response_kind="campaign_enrolment",
        content_mix="balanced",
        default_permission="suggest_only",
        money_ceiling_kes=2000000.0,
    ),
    dict(
        action_code="escalate_to_a_manager",
        title="Send this up to a manager",
        who="A group where the money or the risk is past what a normal response covers",
        evidence_required="A finding a person accepted, with the size that puts it past the limit",
        response_kind="escalate",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
        action_code="change_how_we_treat_them",
        title="Change how these clients are treated from now on",
        who="A group whose standing has changed enough that the old handling is wrong",
        evidence_required="A finding a person accepted, naming what about the group changed",
        response_kind="change_client_state",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
    dict(
        action_code="ask_a_person_first",
        title="Stop and ask a person before anything happens",
        who="A group where the right response is not clear enough to pick one",
        evidence_required="A finding a person accepted, and a reason the response is unclear",
        response_kind="ask_a_person_first",
        content_mix="learning_only",
        default_permission="suggest_only",
    ),
]

_COLUMNS = (
    "version",
    "action_code",
    "title",
    "who",
    "evidence_required",
    "response_kind",
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
    """Every action of this version, with the same value in every column."""
    blank = {name: None for name in _COLUMNS}
    return [
        {**blank, **action, "version": _VERSION, "paused": False, "valid_from": _VALID_FROM}
        for action in _ACTIONS
    ]


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE agent_action_catalog SET valid_to = :valid_from "
            "WHERE version < :version AND valid_to IS NULL"
        ).bindparams(valid_from=_VALID_FROM, version=_VERSION)
    )
    op.bulk_insert(_CATALOG_TABLE, _rows())


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM agent_action_catalog WHERE version = :version").bindparams(
            version=_VERSION
        )
    )
    op.execute(
        sa.text(
            "UPDATE agent_action_catalog SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_VERSION - 1, valid_from=_VALID_FROM)
    )
