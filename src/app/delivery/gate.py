"""The send gate: the one checkpoint a sender must pass before anything goes out.

No real sender exists yet (a provider integration is M10's job), but the
invariant it must honor, nothing sends without approval, has to hold from the
moment a message can be decided, not just from the moment a sender is wired
up. Every gate decision audits, an allow as well as a refusal, so a denied
send attempt is as visible in the trail as an approved one.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.outreach import OutreachMessage


class MessageNotApproved(Exception):
    """The message is not approved and cannot be authorized for sending."""


def authorize_send(session: Session, message: OutreachMessage) -> OutreachMessage:
    """Raise MessageNotApproved unless the message is approved; audits either way."""
    if message.status != "approved":
        record_audit(
            session,
            entity_type="outreach_message",
            action="send_gate_denied",
            entity_id=message.message_id,
            detail={"status": message.status},
        )
        session.flush()
        raise MessageNotApproved(f"{message.message_id} is {message.status}, not approved")

    record_audit(
        session,
        entity_type="outreach_message",
        action="send_gate_allowed",
        entity_id=message.message_id,
    )
    session.flush()
    return message
