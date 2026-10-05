from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.action_catalog import (
    ActionSpec,
    load_active_actions,
    save_action_catalog_version,
)
from app.agents.situation_action_mapping import (
    MappingSpec,
    load_active_mappings,
    save_situation_action_mapping_version,
)
from app.rules.catalog import AngleSpec, load_active_angles, save_catalog_version

revision: str = "b2d5f8a1c4e7"
down_revision: str | Sequence[str] | None = "c8e4a1b6d2f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 7
_PREVIOUS = 6
_MAPPING_VERSION = 5
_PREVIOUS_MAPPING = 4
_VALID_FROM = date(2026, 10, 3)

_NEW_ANGLES = [
    AngleSpec(
        angle="follow_up_when_no_one_called",
        headline="A note from your Cytonn team",
        who=(
            "Someone whose account the team wanted to speak about, where no call has been made yet"
        ),
        claim=(
            "The team would like to check in about the client's account, and this "
            "note is the written version of that call"
        ),
        ask="Invite the client to reply to this email, or to say when a call would suit them",
        never=(
            "Never say we called, tried to call, or missed the client. Never say or "
            "imply the client has gone quiet, lapsed, or done anything wrong. Never "
            "state a balance, a deposit date, an amount, or a count. Never mention a "
            "call list, a risk score, or any internal label. Never add pressure or "
            "urgency. Never promise a return or a rate. Never ask the client to "
            "confirm contact details"
        ),
        use=(
            "Keep it short, warm and personal. Do not use retrieved product or market "
            "facts. Do not name the account manager. Do not state any figure that was "
            "not supplied as a fact"
        ),
        family="investment_habit",
    ),
    AngleSpec(
        angle="suggest_second_fund",
        headline="Another fund, explained",
        who="Someone in good standing who holds one fund only",
        claim=(
            "Many investors use more than one fund because each fund is built for a "
            "different purpose, and it helps to understand what each one is for "
            "before deciding anything"
        ),
        ask=(
            "Invite the client to read the short explanation and to reply with any "
            "questions. Do not ask them to invest, open, or switch to anything"
        ),
        never=(
            "Never tell the client to buy, open, or move money into a fund. Never "
            "name a fund as something the client should choose. Never say or imply "
            "their current fund is a poor choice or is not enough. Never compare "
            "returns. Never promise a return or a rate. Never say the client was "
            "selected or is a good match. Never state their balance, their deposits, "
            "or how long they have been with us. Never ask the client to confirm "
            "contact details"
        ),
        use=(
            "Explain one idea only, using the client guide in the facts. Use a product "
            "fact only when the guide does not cover the point. Present it as "
            "something to learn about, never as advice. Do not state any figure that "
            "was not supplied as a fact"
        ),
        family="fit_and_guidance",
    ),
    AngleSpec(
        angle="ask_what_changed",
        headline="Has anything changed for you?",
        who="Someone who still holds a fund and whose deposits have been getting smaller",
        claim=(
            "The team values the client and would like to understand whether "
            "something has changed for them, so that how we help still fits"
        ),
        ask="Ask one open question: has anything changed that we should know about",
        never=(
            "Never pitch, suggest or name any product, fund, offer or feature. Never "
            "ask the client to deposit or top up. Never say or imply their deposits "
            "have fallen, shrunk or slowed. Never state a balance, an amount, a date "
            "or a count. Never blame the client or imply they are doing something "
            "wrong. Never promise a return or a rate. Never ask the client to "
            "confirm contact details"
        ),
        use=(
            "Keep it to three short sentences at most. Do not use retrieved product "
            "or market facts. Do not state any figure that was not supplied as a fact"
        ),
        family="investment_habit",
    ),
    AngleSpec(
        angle="send_learning_note",
        headline="Something useful to know",
        who="Someone who holds a fund and is in a quiet period, where there is nothing to sell",
        claim=(
            "One short, useful idea about saving or about their account is being "
            "shared because it may help, with nothing asked in return"
        ),
        ask="Make no ask. Say only that the client is welcome to reply with questions",
        never=(
            "Never ask the client to deposit, top up, call, switch or decide "
            "anything. Never include a call to action. Never suggest a product. Never "
            "say or imply the client has gone quiet, lapsed, or done anything wrong. "
            "Never state a balance, an amount, a date or a count. Never explain "
            "anything that is not in the client guide. Never promise a return or a "
            "rate. Never ask the client to confirm contact details"
        ),
        use=(
            "Explain the one idea in the client guide in plain words and stop. Use no "
            "other product or market fact. Do not state any figure that was not "
            "supplied as a fact"
        ),
        family="fit_and_guidance",
    ),
    AngleSpec(
        angle="start_win_back",
        headline="Your account is still here for you",
        who="Someone who holds a very small balance and has not paid in for a long time",
        claim=(
            "The client's account is still open and welcome, and starting again can "
            "be as small and as easy as they like"
        ),
        ask=(
            "Invite the client to add a small amount whenever it suits them, or to "
            "tell us if something got in the way"
        ),
        never=(
            "Never say or imply the client has left, lapsed, gone quiet, or stopped "
            "paying in. Never state or hint at their balance, how small it is, or how "
            "long it has been since they paid in. Never mention the monthly fee. "
            "Never suggest closing the account or taking the money out. Never add "
            "pressure or urgency. Never promise a return or a rate. Never ask the "
            "client to confirm contact details"
        ),
        use=(
            "Keep it short and warm, and make the first step sound easy. A current "
            "product fact may be used only when it makes the next step clearer. Do "
            "not state any figure that was not supplied as a fact"
        ),
        family="reengagement",
    ),
]

_ACTION_CHANGES = {
    "follow_up_when_no_one_called": {},
    "suggest_second_fund": {
        "content_mix": "mostly_learning",
        "evidence_required": (
            "A risk band of none or low, exactly one fund held by that client, and an "
            "approved client guide for this action"
        ),
    },
    "ask_what_changed": {"content_mix": "mostly_ask"},
    "send_learning_note": {
        "evidence_required": (
            "No other action applies, the client is not held or suppressed, and an "
            "approved client guide for this action"
        ),
    },
    "start_win_back": {},
}

_SITUATION_ANGLES = {
    "waiting_on_a_call": "follow_up_when_no_one_called",
    "healthy_one_fund": "suggest_second_fund",
    "getting_smaller": "ask_what_changed",
    "very_small_and_quiet": "start_win_back",
}

_PREVIOUS_VERSIONS = (
    ("message_angle_catalog", _PREVIOUS),
    ("agent_action_catalog", _PREVIOUS),
    ("situation_action_mapping", _PREVIOUS_MAPPING),
)


def _angle_spec(row) -> AngleSpec:
    return AngleSpec(
        angle=row.angle,
        headline=row.headline,
        who=row.who,
        claim=row.claim,
        ask=row.ask,
        never=row.never,
        use=row.use,
        held=row.held,
        cta=row.cta,
        family=row.family,
        tone=row.tone,
    )


def _mapping_spec(row) -> MappingSpec:
    return MappingSpec(
        situation=row.situation,
        action_code=row.action_code,
        objective=row.objective,
        angle=_SITUATION_ANGLES.get(row.situation, row.angle),
        evidence_required=row.evidence_required,
        channel=row.channel,
        priority=row.priority,
    )


def _action_spec(row) -> ActionSpec:
    spec = ActionSpec(
        action_code=row.action_code,
        title=row.title,
        who=row.who,
        evidence_required=row.evidence_required,
        response_kind=row.response_kind,
        content_mix=row.content_mix,
        default_permission=row.default_permission,
        message_angle=row.message_angle,
        channel=row.channel,
        money_ceiling_kes=row.money_ceiling_kes,
        paused=row.paused,
    )
    if row.action_code not in _ACTION_CHANGES:
        return spec
    return replace(
        spec,
        paused=False,
        message_angle=row.action_code,
        default_permission="suggest_only",
        **_ACTION_CHANGES[row.action_code],
    )


def upgrade() -> None:
    session = Session(bind=op.get_bind())

    angles = [_angle_spec(row) for row in load_active_angles(session, _VALID_FROM).values()]
    save_catalog_version(session, _VERSION, [*angles, *_NEW_ANGLES], valid_from=_VALID_FROM)
    session.execute(
        sa.text(
            "UPDATE message_angle_catalog SET valid_to = :valid_from "
            "WHERE version = :previous AND valid_to IS NULL"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )

    actions = [_action_spec(row) for row in load_active_actions(session, _VALID_FROM).values()]
    save_action_catalog_version(session, _VERSION, actions, valid_from=_VALID_FROM)

    mappings = [_mapping_spec(row) for row in load_active_mappings(session, _VALID_FROM)]
    save_situation_action_mapping_version(
        session, _MAPPING_VERSION, mappings, valid_from=_VALID_FROM
    )
    session.flush()


def downgrade() -> None:
    for table, version in (
        ("message_angle_catalog", _VERSION),
        ("agent_action_catalog", _VERSION),
        ("situation_action_mapping", _MAPPING_VERSION),
    ):
        op.execute(sa.text(f"DELETE FROM {table} WHERE version = {version}"))

    op.execute(
        sa.text(
            "UPDATE message_angle_catalog SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
    op.execute(
        sa.text(
            "UPDATE agent_action_catalog SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
    op.execute(
        sa.text(
            "UPDATE situation_action_mapping SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS_MAPPING, valid_from=_VALID_FROM)
    )

    for component_type, previous in _PREVIOUS_VERSIONS:
        op.execute(
            sa.text(
                "UPDATE active_configuration SET active_version = :previous "
                "WHERE component_type = :component_type AND active_version > :previous"
            ).bindparams(previous=previous, component_type=component_type)
        )
    new_angles = ", ".join(f"'{angle.angle}'" for angle in _NEW_ANGLES)
    op.execute(
        sa.text(
            "DELETE FROM active_configuration WHERE component_type = 'message_angle_catalog' "
            f"AND component_key IN ({new_angles})"
        )
    )
