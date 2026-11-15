from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.agent_loop import start_agent_run
from app.config import get_settings
from app.db.models.active_clients import CONTACT_INTERACTION_TYPES, ActiveClientInteraction
from app.db.models.agent_run import CHAT_AGENT, AgentRun
from app.db.models.assistant_starter import AssistantStarter
from app.db.models.chat import ChatSession, ChatTurn
from app.db.models.complaints import ClientComplaint
from app.db.models.suppression import Suppression
from app.schemas.assistant_blocks import DRAFT_EMAIL, parse_action_token, public_blocks
from app.schemas.client_assistant import (
    AssistantAboutOut,
    AssistantSessionOut,
    AssistantSessionSummaryOut,
    AssistantTurnOut,
    StarterOut,
)
from app.services.email_options import OptionNotFound, find_chosen_option

QUICK_EMAIL = "quick_email"
EMAIL_OPTIONS = "email_options"
DEEPER_BRIEF = "deeper_brief"
FREE = "free"

MAX_TITLE_CHARS = 120
DEFAULT_SESSION_LIMIT = 30

EMAIL_OFF = "AI email drafts are switched off in this environment."
EMAIL_BLOCKED = "This client has an open complaint or asked not to be contacted."
ABOUT_TITLE = "Client copilot"
ABOUT_SUBTITLE = "Grounded in this client's verified activity"
READS_LABEL = "Using"
GREETING = "What would you like to prepare for this client?"
FOOTNOTE = (
    "The assistant sees bands and tiers, not the client's name or exact figures. "
    "Always review before sending."
)

Availability = Callable[[Session, int, int], str | None]


class AssistantSessionNotFound(Exception):
    pass


class StarterNotFound(Exception):
    pass


class StarterUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BadTurnRequest(Exception):
    pass


def email_unavailable_reason(session: Session, client_id: int, unit_fund_id: int) -> str | None:
    if not get_settings().ai_briefing_enabled:
        return EMAIL_OFF
    complaint = session.scalar(
        select(ClientComplaint.id)
        .where(ClientComplaint.client_id == client_id, ClientComplaint.status == "open")
        .limit(1)
    )
    if complaint is not None or session.get(Suppression, client_id) is not None:
        return EMAIL_BLOCKED
    return None


def _always_available(session: Session, client_id: int, unit_fund_id: int) -> str | None:
    return None


AVAILABILITY: dict[str, Availability] = {
    QUICK_EMAIL: email_unavailable_reason,
    EMAIL_OPTIONS: email_unavailable_reason,
    DEEPER_BRIEF: _always_available,
}


def _active_starters(session: Session) -> list[AssistantStarter]:
    return list(
        session.scalars(
            select(AssistantStarter)
            .where(AssistantStarter.active.is_(True))
            .order_by(AssistantStarter.sort_order, AssistantStarter.code)
        ).all()
    )


def list_starters(session: Session, client_id: int, unit_fund_id: int) -> list[StarterOut]:
    starters = []
    for row in _active_starters(session):
        check = AVAILABILITY.get(row.intent)
        if check is None:
            continue
        reason = check(session, client_id, unit_fund_id)
        starters.append(
            StarterOut(
                code=row.code,
                label=row.label,
                description=row.description,
                icon=row.icon,
                enabled=reason is None,
                disabled_reason=reason,
            )
        )
    return starters


def starters_for_follow_up(
    session: Session, client_id: int, unit_fund_id: int, *, skip_intent: str | None = None
) -> list[dict[str, str]]:
    found = []
    for row in _active_starters(session):
        check = AVAILABILITY.get(row.intent)
        if check is None or row.intent == skip_intent:
            continue
        if check(session, client_id, unit_fund_id) is None:
            found.append({"code": row.code, "label": row.label})
    return found


def _owned_session(
    session: Session,
    session_id: int,
    *,
    client_id: int,
    unit_fund_id: int,
    username: str,
    sees_every_book: bool,
) -> ChatSession:
    row = session.get(ChatSession, session_id)
    if (
        row is None
        or row.client_id != client_id
        or row.unit_fund_id != unit_fund_id
        or not (sees_every_book or row.reviewer_id == username)
    ):
        raise AssistantSessionNotFound(session_id)
    return row


def _resolve_request(
    session: Session,
    *,
    client_id: int,
    unit_fund_id: int,
    username: str,
    sees_every_book: bool,
    starter_code: str | None,
    action: str | None,
    text: str | None,
) -> tuple[str, str, str | None]:
    if starter_code:
        starter = session.get(AssistantStarter, starter_code)
        check = AVAILABILITY.get(starter.intent) if starter and starter.active else None
        if check is None:
            raise StarterNotFound(starter_code)
        reason = check(session, client_id, unit_fund_id)
        if reason is not None:
            raise StarterUnavailable(reason)
        return starter.intent, starter.label, None
    if action:
        try:
            kind, turn_id, option_id = parse_action_token(action)
        except ValueError as exc:
            raise BadTurnRequest(str(exc)) from exc
        if kind != DRAFT_EMAIL:
            raise BadTurnRequest("the action was not recognised")
        reason = email_unavailable_reason(session, client_id, unit_fund_id)
        if reason is not None:
            raise StarterUnavailable(reason)
        try:
            chosen = find_chosen_option(
                session,
                client_id=client_id,
                unit_fund_id=unit_fund_id,
                turn_id=turn_id,
                option_id=option_id,
                username=username,
                sees_every_book=sees_every_book,
            )
        except OptionNotFound as exc:
            raise BadTurnRequest("that option is no longer available") from exc
        return DRAFT_EMAIL, f'Write the "{chosen.title}" email', action
    return FREE, (text or "").strip(), None


def start_client_turn(
    session: Session,
    *,
    client_id: int,
    unit_fund_id: int,
    username: str,
    sees_every_book: bool,
    fa_id: str | None,
    session_id: int | None,
    starter_code: str | None,
    action: str | None,
    text: str | None,
) -> ChatTurn:
    intent, question, action_ref = _resolve_request(
        session,
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        username=username,
        sees_every_book=sees_every_book,
        starter_code=starter_code,
        action=action,
        text=text,
    )
    if not question:
        raise BadTurnRequest("the message was empty")
    if session_id is None:
        chat_session = ChatSession(
            reviewer_id=username,
            client_id=client_id,
            unit_fund_id=unit_fund_id,
            title=question[:MAX_TITLE_CHARS],
        )
        session.add(chat_session)
        session.flush()
        session_id = chat_session.session_id
    else:
        _owned_session(
            session,
            session_id,
            client_id=client_id,
            unit_fund_id=unit_fund_id,
            username=username,
            sees_every_book=sees_every_book,
        )
    run = start_agent_run(session, trigger="chat", kind=CHAT_AGENT, fa_id=fa_id)
    turn = ChatTurn(
        session_id=session_id,
        run_id=run.run_id,
        question=question,
        intent=intent,
        action_ref=action_ref,
    )
    session.add(turn)
    session.commit()
    return turn


def _turn_out(turn: ChatTurn, run_state: str | None) -> AssistantTurnOut:
    return AssistantTurnOut(
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        question=turn.question,
        intent=turn.intent,
        blocks=public_blocks(turn.blocks),
        run_state=run_state,
        created_at=turn.created_at,
    )


def get_client_session(
    session: Session,
    session_id: int,
    *,
    client_id: int,
    unit_fund_id: int,
    username: str,
    sees_every_book: bool,
) -> AssistantSessionOut:
    row = _owned_session(
        session,
        session_id,
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        username=username,
        sees_every_book=sees_every_book,
    )
    turns = session.execute(
        select(ChatTurn, AgentRun.state)
        .join(AgentRun, AgentRun.run_id == ChatTurn.run_id)
        .where(ChatTurn.session_id == session_id)
        .order_by(ChatTurn.turn_id)
    ).all()
    return AssistantSessionOut(
        session_id=row.session_id,
        started_at=row.started_at,
        title=row.title,
        turns=[_turn_out(turn, state) for turn, state in turns],
    )


def list_client_sessions(
    session: Session,
    *,
    client_id: int,
    unit_fund_id: int,
    username: str,
    sees_every_book: bool,
    limit: int = DEFAULT_SESSION_LIMIT,
) -> list[AssistantSessionSummaryOut]:
    query = (
        select(ChatSession, func.count(ChatTurn.turn_id))
        .outerjoin(ChatTurn, ChatTurn.session_id == ChatSession.session_id)
        .where(ChatSession.client_id == client_id, ChatSession.unit_fund_id == unit_fund_id)
        .group_by(ChatSession.session_id)
        .order_by(ChatSession.session_id.desc())
        .limit(limit)
    )
    if not sees_every_book:
        query = query.where(ChatSession.reviewer_id == username)
    return [
        AssistantSessionSummaryOut(
            session_id=row.session_id,
            started_at=row.started_at,
            title=row.title,
            turn_count=count,
        )
        for row, count in session.execute(query).all()
    ]


def assistant_about(session: Session, client_id: int, unit_fund_id: int) -> AssistantAboutOut:
    contacts = (
        session.scalar(
            select(func.count())
            .select_from(ActiveClientInteraction)
            .where(
                ActiveClientInteraction.client_id == client_id,
                ActiveClientInteraction.unit_fund_id == unit_fund_id,
                ActiveClientInteraction.type.in_(CONTACT_INTERACTION_TYPES),
            )
        )
        or 0
    )
    past = "No past interactions" if contacts == 0 else f"Past interactions ({contacts})"
    return AssistantAboutOut(
        title=ABOUT_TITLE,
        subtitle=ABOUT_SUBTITLE,
        greeting=GREETING,
        reads_label=READS_LABEL,
        reads=["Verified brief", past, "Deposits and withdrawals"],
        footnote=FOOTNOTE,
    )
