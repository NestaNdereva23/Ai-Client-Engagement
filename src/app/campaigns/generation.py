"""Wire the real generation pipeline into a campaign touch, and into a
reviewer's request to regenerate a still-pending draft.

campaigns.touch expects a GenerateFn: something that turns one enrollment's
due step into an OutreachMessage. Every other module in this package
(scheduler, eligibility, state_machine) stayed channel- and model-agnostic on
purpose; this is the one place that actually builds a ChannelAgent's draft
and persists it, the production counterpart to a test's fake_generate.
"""

from __future__ import annotations

import functools

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.email_agent import build_system_prompt
from app.agents.email_channel import build_default_agent
from app.agents.orchestrator import Orchestrator
from app.audit.log import record_audit
from app.campaigns.cohorts import CohortSlot
from app.config import Settings
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan
from app.db.models.models import ClientFeatures
from app.db.models.outreach import Campaign, OutreachMessage
from app.llmops.operations import active_tracer
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.trace_names import CAMPAIGN_EMAIL_DRAFT, TAG_CAMPAIGN, TAG_EMAIL
from app.llmops.tracing import Tracer, trace_scope
from app.llmops.versions import persist_generation_run
from app.privacy.boundary import AuditSink, BoundaryAudit
from app.services.review import create_outreach_message, get_message

_REPEAT_EXCERPT_LIMIT = 400


class MessageNotRegenerable(Exception):
    """The message has already been decided, so its draft may not be replaced."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


class RegenerationRejected(Exception):
    """The fresh attempt was itself rejected; the original message is untouched."""

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        super().__init__(message_id)


def resolve_product(session: Session, client_id: int) -> str:
    """The RAG-facing product string for a client, derived from their fund
    type band ("money_market" -> "money market"), the same vocabulary
    rag.retrieve's section lookup matches against. "other" (an unrecognised
    or not-yet-computed fund type) matches no section and simply retrieves
    no chunks, the same graceful-empty path a client with no facts row at
    all already takes.
    """
    fund_type = session.execute(
        select(ClientFeatures.fund_type).where(ClientFeatures.client_id == client_id)
    ).scalar_one_or_none()
    return (fund_type or "other").replace("_", " ")


def resolve_touch_channel(session: Session, campaign_id: int, step_no: int) -> str:
    # The step's own channel wins; the campaign's default is the fallback.
    step_channel = session.execute(
        select(CampaignStep.channel).where(
            CampaignStep.campaign_id == campaign_id, CampaignStep.step_no == step_no
        )
    ).scalar_one_or_none()
    if step_channel is not None:
        return step_channel
    return session.execute(
        select(Campaign.default_channel).where(Campaign.campaign_id == campaign_id)
    ).scalar_one()


def model_boundary_audit_sink(session: Session) -> AuditSink:
    """A real AuditSink: one audit_log row per model boundary crossing."""

    def sink(audit: BoundaryAudit) -> None:
        record_audit(
            session,
            entity_type="model_boundary",
            action="cross",
            entity_id=audit.entity_id,
            run_id=audit.run_id,
            trace_id=audit.trace_id,
            detail={
                "fields": audit.fields,
                "inbound": audit.inbound,
                "outbound": audit.outbound,
                "reason": audit.reason,
            },
        )

    return sink


def _generate_and_persist(
    session: Session,
    *,
    client_id: int,
    campaign_id: int,
    orchestrator: Orchestrator,
    channel: str,
    settings: Settings,
    tracer: Tracer | None,
    cohort_slot: CohortSlot | None = None,
) -> OutreachMessage | None:
    """Run the graph for one client and persist the result.

    Returns the created OutreachMessage on an accepted draft, or None when
    every retry was rejected. The run itself is always persisted and
    stamped either way, so a rejection still shows up in guardrail metrics
    even though there is nothing yet to review.

    cohort_slot is passed straight through to create_outreach_message; see
    its docstring. regenerate_message uses this to keep a replacement
    message in the same cohort slot as the one it's replacing.
    """
    product = resolve_product(session, client_id)
    with trace_scope(
        tracer or active_tracer(),
        name=CAMPAIGN_EMAIL_DRAFT,
        tags=[TAG_CAMPAIGN, TAG_EMAIL],
    ):
        state = orchestrator.generate(channel, client_id=client_id, product=product)

    run = persist_generation_run(session, state, settings, channel=channel)
    session.flush()
    persist_generation_telemetry(session, run, state, tracer=tracer)

    if state.get("status") != "accepted":
        return None
    return create_outreach_message(
        session,
        run,
        campaign_id=campaign_id,
        channel=channel,
        call_brief=state.get("call_brief"),
        cohort_slot=cohort_slot,
    )


def _touch_plan_for(session: Session, enrollment_id: int, step_no: int) -> TouchPlan | None:
    return session.execute(
        select(TouchPlan).where(
            TouchPlan.enrollment_id == enrollment_id, TouchPlan.step_no == step_no
        )
    ).scalar_one_or_none()


def _repeat_prohibition(session: Session, enrollment_id: int, step_no: int) -> str | None:
    # The previous step's own draft, already pre-reattachment and model-safe.
    previous = session.execute(
        select(OutreachMessage.ai_draft_content)
        .join(TouchLog, TouchLog.message_id == OutreachMessage.message_id)
        .where(TouchLog.enrollment_id == enrollment_id, TouchLog.step_no == step_no - 1)
    ).scalar_one_or_none()
    if not previous:
        return None
    body = str(previous.get("body") or previous.get("subject") or "")[:_REPEAT_EXCERPT_LIMIT]
    if not body:
        return None
    return f"repeat the wording of this client's last email to them: {body}"


def _planned_orchestrator(
    session: Session,
    *,
    plan: TouchPlan,
    enrollment_id: int,
    settings: Settings,
    tracer: Tracer | None,
) -> Orchestrator:
    prohibition = _repeat_prohibition(session, enrollment_id, plan.step_no)
    extra_prohibitions = (prohibition,) if prohibition else ()
    orchestrator = Orchestrator()
    orchestrator.register(
        build_default_agent(
            session,
            settings,
            audit=model_boundary_audit_sink(session),
            tracer=tracer,
            prompt_builder=functools.partial(
                build_system_prompt, extra_prohibitions=extra_prohibitions
            ),
            angle_override=plan.angle,
        )
    )
    return orchestrator


def generate_for_enrollment(
    session: Session,
    enrollment: Enrollment,
    step_no: int,
    *,
    orchestrator: Orchestrator,
    channel: str | None = None,
    settings: Settings,
    tracer: Tracer | None = None,
) -> OutreachMessage | None:
    """The real GenerateFn campaigns.touch expects: one enrollment's due step
    turned into a persisted, pending-review OutreachMessage, or None if
    every guardrail retry rejected the draft. campaigns.touch leaves the
    touch's message_id null in that case; see generate_touch's docstring
    for why that does not make the step retryable on its own.

    channel defaults to whatever this step itself resolves to, so a batch
    run over mixed touches drafts each one on its own channel.
    """
    channel = channel or resolve_touch_channel(session, enrollment.campaign_id, step_no)
    plan = (
        _touch_plan_for(session, enrollment.enrollment_id, step_no) if channel == "email" else None
    )
    if plan is not None:
        orchestrator = _planned_orchestrator(
            session,
            plan=plan,
            enrollment_id=enrollment.enrollment_id,
            settings=settings,
            tracer=tracer,
        )
    return _generate_and_persist(
        session,
        client_id=enrollment.client_id,
        campaign_id=enrollment.campaign_id,
        orchestrator=orchestrator,
        channel=channel,
        settings=settings,
        tracer=tracer,
    )


def regenerate_message(
    session: Session,
    message_id: str,
    *,
    orchestrator: Orchestrator,
    settings: Settings,
    tracer: Tracer | None = None,
) -> OutreachMessage:
    """Replace a still-pending message's draft with a freshly generated one.

    Only pending_review may be regenerated: once decide() has ever run on a
    message it has written a review_action, and rewriting the content
    underneath that history would leave a recorded outcome pointing at a
    draft that no longer exists. Raises RegenerationRejected, leaving the
    original message exactly as it was, when the fresh attempt is itself
    rejected by every guardrail retry -- nothing to replace it with is
    worse than the draft already in review.

    The replacement is drafted on the same channel as the message it
    replaces; regenerating never changes what channel a message goes out on.

    A message reached from a campaign has a touch_log row pointing at it.
    That touch is still the same touch, the same enrollment's same step, so
    it is repointed at the replacement rather than left behind: the
    eligibility gate reads the latest touch's message to decide whether the
    step is still waiting on a decision, and a touch left pointing at a
    deleted message would read as an unresolved one forever.
    """
    message = get_message(session, message_id)
    if message.status != "pending_review":
        raise MessageNotRegenerable(message.status)

    fresh = _generate_and_persist(
        session,
        client_id=message.client_id,
        campaign_id=message.campaign_id,
        orchestrator=orchestrator,
        channel=message.channel,
        settings=settings,
        tracer=tracer,
        cohort_slot=CohortSlot(cohort_id=message.cohort_id, is_sample=message.is_sample),
    )
    if fresh is None:
        raise RegenerationRejected(message_id)

    session.execute(
        update(TouchLog)
        .where(TouchLog.message_id == message_id)
        .values(message_id=fresh.message_id)
    )
    session.delete(message)
    session.flush()
    record_audit(
        session,
        entity_type="outreach_message",
        action="regenerate",
        entity_id=fresh.message_id,
        detail={"replaced_message_id": message_id},
    )
    return fresh
