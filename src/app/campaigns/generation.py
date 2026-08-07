"""Wire the real generation pipeline into a campaign touch, and into a
reviewer's request to regenerate a still-pending draft.

campaigns.touch expects a GenerateFn: something that turns one enrollment's
due step into an OutreachMessage. Every other module in this package
(scheduler, eligibility, state_machine) stayed channel- and model-agnostic on
purpose; this is the one place that actually builds a ChannelAgent's draft
and persists it, the production counterpart to a test's fake_generate.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.orchestrator import ChannelAgent
from app.audit.log import record_audit
from app.config import Settings
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.models import ClientFeatures
from app.db.models.outreach import OutreachMessage
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.tracing import Tracer
from app.llmops.versions import persist_generation_run
from app.privacy.boundary import AuditSink, BoundaryAudit
from app.services.review import create_outreach_message, get_message


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
    agent: ChannelAgent,
    settings: Settings,
    tracer: Tracer | None,
) -> OutreachMessage | None:
    """Run the graph for one client and persist the result.

    Returns the created OutreachMessage on an accepted draft, or None when
    every retry was rejected. The run itself is always persisted and
    stamped either way, so a rejection still shows up in guardrail metrics
    even though there is nothing yet to review.
    """
    product = resolve_product(session, client_id)
    state = agent.generate(client_id=client_id, product=product)

    run = persist_generation_run(session, state, settings)
    session.flush()
    persist_generation_telemetry(session, run, state, tracer=tracer)

    if state.get("status") != "accepted":
        return None
    return create_outreach_message(session, run, campaign_id=campaign_id)


def generate_for_enrollment(
    session: Session,
    enrollment: Enrollment,
    step_no: int,
    *,
    agent: ChannelAgent,
    settings: Settings,
    tracer: Tracer | None = None,
) -> OutreachMessage | None:
    """The real GenerateFn campaigns.touch expects: one enrollment's due step
    turned into a persisted, pending-review OutreachMessage, or None if
    every guardrail retry rejected the draft. campaigns.touch leaves the
    touch's message_id null in that case; see generate_touch's docstring
    for why that does not make the step retryable on its own.
    """
    return _generate_and_persist(
        session,
        client_id=enrollment.client_id,
        campaign_id=enrollment.campaign_id,
        agent=agent,
        settings=settings,
        tracer=tracer,
    )


def regenerate_message(
    session: Session,
    message_id: str,
    *,
    agent: ChannelAgent,
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
        agent=agent,
        settings=settings,
        tracer=tracer,
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
