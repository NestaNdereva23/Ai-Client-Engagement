from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.progress import StepProgress
from app.agents.prompt_versioning import EMAIL_OPTIONS_SUGGEST, prompt_text
from app.audit.log import record_audit
from app.briefing.email_options import (
    EmailOption,
    OptionsRejected,
    check_angle,
    suggest_email_options,
)
from app.config import get_settings
from app.db.models.chat import ChatSession, ChatTurn
from app.db.models.suppression import Suppression
from app.llmops.operations import trace_operation
from app.llmops.trace_names import SUGGEST_EMAIL_OPTIONS
from app.privacy.boundary import AuditSink, BoundaryAudit
from app.privacy.llm_client import LLMClient
from app.schemas.assistant_blocks import ANGLE_KEY
from app.services.assistant_facts import build_assistant_facts
from app.services.briefing import BriefingNotFound, gather_briefing_facts
from app.services.client_email_draft import EmailDraftBlocked, EmailDraftDisabled
from app.services.client_touchpoints import today_in_nairobi

ENTITY_TYPE = "client_email_options"
MODEL_CROSSING = "suggest"
OPTIONS_BLOCK = "options"


class EmailOptionsFailed(Exception):
    pass


class OptionNotFound(Exception):
    pass


@dataclass(frozen=True)
class ChosenOption:
    title: str
    angle: str


def _crossing_sink(session: Session, username: str) -> AuditSink:
    def sink(record: BoundaryAudit) -> None:
        record_audit(
            session,
            entity_type=ENTITY_TYPE,
            action=MODEL_CROSSING,
            entity_id=record.entity_id,
            actor_id=username,
            detail={
                "fields": record.fields,
                "inbound": record.inbound,
                "outbound": record.outbound,
                "reason": record.reason,
            },
        )

    return sink


def suggest_options_for_client(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    username: str,
    llm_client: LLMClient,
    steps: StepProgress | None = None,
) -> list[EmailOption]:
    steps = steps or StepProgress()
    if not get_settings().ai_briefing_enabled:
        raise EmailDraftDisabled()
    today = today_in_nairobi()
    with steps.step("Reading the verified brief"):
        facts = gather_briefing_facts(session, client_id, unit_fund_id, today)
        if facts is None:
            raise BriefingNotFound(f"{client_id}/{unit_fund_id}")
    with steps.step("Checking complaints and do not contact"):
        if facts.has_open_complaint or session.get(Suppression, client_id) is not None:
            raise EmailDraftBlocked()
    with steps.step("Reading past interactions"):
        block = build_assistant_facts(session, facts, client_id, unit_fund_id, today)
    try:
        with (
            steps.step("Choosing the kinds of email that fit"),
            trace_operation(SUGGEST_EMAIL_OPTIONS) as operation,
        ):
            options = suggest_email_options(
                block,
                llm_client,
                entity_id=str(client_id),
                audit=_crossing_sink(session, username),
                goal=facts.tier_goal,
                approach=facts.tier_action,
                instructions=prompt_text(session, EMAIL_OPTIONS_SUGGEST, today),
            )
            operation.output = {"option_titles": [option.title for option in options]}
    except OptionsRejected as exc:
        session.commit()
        raise EmailOptionsFailed() from exc
    session.commit()
    return options


def find_chosen_option(
    session: Session,
    *,
    client_id: int,
    unit_fund_id: int,
    turn_id: int,
    option_id: str,
    username: str,
    sees_every_book: bool,
) -> ChosenOption:
    row = session.execute(
        select(ChatTurn.blocks, ChatSession.reviewer_id)
        .join(ChatSession, ChatSession.session_id == ChatTurn.session_id)
        .where(
            ChatTurn.turn_id == turn_id,
            ChatSession.client_id == client_id,
            ChatSession.unit_fund_id == unit_fund_id,
        )
    ).first()
    if row is None or not (sees_every_book or row.reviewer_id == username):
        raise OptionNotFound(option_id)
    for block in row.blocks:
        if block.get("type") != OPTIONS_BLOCK:
            continue
        for item in block.get("items", []):
            if item.get("id") == option_id and item.get(ANGLE_KEY):
                try:
                    return ChosenOption(item["title"], check_angle(item[ANGLE_KEY]))
                except OptionsRejected:
                    raise OptionNotFound(option_id) from None
    raise OptionNotFound(option_id)
