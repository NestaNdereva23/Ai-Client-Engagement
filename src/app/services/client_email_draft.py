from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.progress import StepProgress
from app.agents.prompt_versioning import CLIENT_EMAIL_DRAFT, prompt_text
from app.audit.log import record_audit, with_on_behalf_of
from app.briefing.client_email import DraftRejected, EmailDraft, draft_client_email
from app.briefing.email_prompts import purpose_key
from app.campaigns.enrollment import enroll_cohort
from app.campaigns.nurture_bridge import ensure_active_client_row
from app.config import get_settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun, PromptVersion
from app.db.models.outreach import AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign
from app.db.models.suppression import Suppression
from app.llmops.operations import trace_operation
from app.llmops.trace_names import CLIENT_EMAIL_DRAFT_TRACE, CLIENT_EMAIL_SAVE, TAG_EMAIL
from app.llmops.tracing import Tracer
from app.llmops.versions import get_or_create_model_version
from app.privacy.boundary import AuditSink, BoundaryAudit
from app.privacy.llm_client import LLMClient, resolve_briefing_model_config
from app.services.assistant_facts import build_assistant_facts
from app.services.briefing import BriefingNotFound, gather_briefing_facts
from app.services.client_next_actions import next_action_for_client
from app.services.client_touchpoints import today_in_nairobi
from app.services.review import create_outreach_message

ENTITY_TYPE = "client_email_draft"
MODEL_CROSSING = "draft"
DRAFT_MADE = "generated"
QUEUE_CAMPAIGN_NAME = "Advisor requested email drafts"
PROMPT_VARIANT = "advisor_requested"


class EmailDraftDisabled(Exception):
    pass


class EmailDraftBlocked(Exception):
    pass


class DailyDraftLimitReached(Exception):
    pass


class EmailDraftFailed(Exception):
    pass


@dataclass(frozen=True)
class ClientEmailDraft:
    message_id: str
    drafts_left_today: int
    subject: str = ""


def _drafts_made_today(session: Session, username: str) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_type == ENTITY_TYPE,
                AuditLog.action == MODEL_CROSSING,
                AuditLog.actor_id == username,
                func.date(AuditLog.created_at) == func.current_date(),
            )
        )
        or 0
    )


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


def _queue_campaign(session: Session) -> Campaign:
    existing = session.scalar(
        select(Campaign)
        .where(
            Campaign.campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE,
            Campaign.name == QUEUE_CAMPAIGN_NAME,
        )
        .order_by(Campaign.campaign_id)
        .limit(1)
    )
    if existing is not None:
        return existing
    campaign = Campaign(
        name=QUEUE_CAMPAIGN_NAME,
        campaign_type=AGENT_PROPOSAL_CAMPAIGN_TYPE,
        status="running",
        start_date=today_in_nairobi(),
    )
    session.add(campaign)
    session.flush()
    return campaign


def _prompt_version(session: Session, purpose: str | None, instructions: str) -> PromptVersion:
    variant = f"{PROMPT_VARIANT}_{purpose}" if purpose else PROMPT_VARIANT
    template_hash = hashlib.sha256(f"email|{variant}|{instructions}".encode()).hexdigest()
    existing = session.scalar(
        select(PromptVersion).where(PromptVersion.template_hash == template_hash)
    )
    if existing is not None:
        return existing
    row = PromptVersion(
        channel="email",
        prompt_variant=variant,
        angle="",
        template_text=instructions,
        template_hash=template_hash,
    )
    session.add(row)
    session.flush()
    return row


def _record_run(
    session: Session, client_id: int, draft: EmailDraft, purpose: str | None, instructions: str
) -> GenerationRun:
    provider, model, temperature, max_tokens = resolve_briefing_model_config(get_settings())
    model_version = get_or_create_model_version(
        session, provider=provider, model_id=model, temperature=temperature, max_tokens=max_tokens
    )
    run = GenerationRun(
        run_id=uuid.uuid4().hex,
        client_id=client_id,
        data_date=today_in_nairobi(),
        prompt_version_id=_prompt_version(session, purpose, instructions).prompt_version_id,
        model_version_id=model_version.model_version_id,
        status="accepted",
        attempts=1,
        ai_draft_content={"subject": draft.subject, "body": draft.body},
    )
    session.add(run)
    session.flush()
    return run


def _queue_for_review(
    session: Session, client_id: int, draft: EmailDraft, purpose: str | None, instructions: str
) -> str:
    if ensure_active_client_row(session, client_id) is None:
        raise EmailDraftFailed()
    run = _record_run(session, client_id, draft, purpose, instructions)
    campaign_id = _queue_campaign(session).campaign_id
    enroll_cohort(session, campaign_id=campaign_id, client_ids=[client_id])
    message = create_outreach_message(session, run, campaign_id=campaign_id)
    if message is None:
        raise EmailDraftFailed()
    return message.message_id


def draft_email_for_client(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    username: str,
    llm_client: LLMClient,
    on_behalf_of: str | None = None,
    angle: str | None = None,
    steps: StepProgress | None = None,
    tracer: Tracer | None = None,
) -> ClientEmailDraft:
    with trace_operation(
        CLIENT_EMAIL_DRAFT_TRACE,
        tracer=tracer,
        input={"basis": "chosen option" if angle else "next action"},
        metadata={"unit_fund_id": unit_fund_id},
        tags=[TAG_EMAIL],
        user_id=username,
    ) as operation:
        draft = _draft_email_for_client(
            session,
            client_id,
            unit_fund_id,
            username=username,
            llm_client=llm_client,
            on_behalf_of=on_behalf_of,
            angle=angle,
            steps=steps,
        )
        operation.output = {"message_id": draft.message_id, "subject": draft.subject}
        return draft


def _draft_email_for_client(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    username: str,
    llm_client: LLMClient,
    on_behalf_of: str | None,
    angle: str | None,
    steps: StepProgress | None,
) -> ClientEmailDraft:
    steps = steps or StepProgress()
    settings = get_settings()
    if not settings.ai_briefing_enabled:
        raise EmailDraftDisabled()
    facts = gather_briefing_facts(session, client_id, unit_fund_id, today_in_nairobi())
    if facts is None:
        raise BriefingNotFound(f"{client_id}/{unit_fund_id}")
    if facts.has_open_complaint or session.get(Suppression, client_id) is not None:
        raise EmailDraftBlocked()

    action = next_action_for_client(session, client_id, unit_fund_id).action
    purpose = action.email_purpose if action is not None else None
    today = today_in_nairobi()
    instructions = prompt_text(session, CLIENT_EMAIL_DRAFT, today)
    purpose_text = (
        prompt_text(session, purpose_key(purpose), today) if purpose and not angle else None
    )

    cap = settings.client_email_draft_daily_cap
    made_today = _drafts_made_today(session, username)
    if made_today >= cap:
        raise DailyDraftLimitReached()

    try:
        with steps.step("Writing the draft"):
            draft = draft_client_email(
                build_assistant_facts(session, facts, client_id, unit_fund_id, today),
                llm_client,
                entity_id=str(client_id),
                audit=_crossing_sink(session, username),
                purpose=None if angle else purpose,
                angle=angle,
                instructions=instructions,
                purpose_text=purpose_text,
            )
    except DraftRejected as exc:
        session.commit()
        raise EmailDraftFailed() from exc

    try:
        with steps.step("Saving it to the Review Queue"), trace_operation(CLIENT_EMAIL_SAVE):
            message_id = _queue_for_review(
                session, client_id, draft, "option" if angle else purpose, instructions
            )
    except EmailDraftFailed:
        session.commit()
        raise
    record_audit(
        session,
        entity_type=ENTITY_TYPE,
        action=DRAFT_MADE,
        entity_id=f"{client_id}/{unit_fund_id}",
        actor_id=username,
        detail=with_on_behalf_of({"message_id": message_id}, on_behalf_of),
    )
    session.commit()
    return ClientEmailDraft(
        message_id=message_id, drafts_left_today=cap - made_today - 1, subject=draft.subject
    )
