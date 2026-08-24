"""Instantiation: turn an approved template into one outreach_message per
matching client.

A template must be approved before any instance is written from it --
services.review.instantiate_message enforces that. Membership is
re-derived, not replayed from draft time: message_template stores only the
profile, not which enrollments were in it, so a client who becomes due for
the same profile later is picked up too.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from app.agents.graph import ContextLoader
from app.campaigns.bucketing import derive_buckets
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.touch import record_touch
from app.db.models.message_template import MessageTemplate
from app.db.models.outreach import OutreachMessage
from app.services.review import TemplateNotApproved, instantiate_message

logger = structlog.get_logger(__name__)


def instantiate_template(
    session: Session,
    template: MessageTemplate,
    *,
    campaign_id: int,
    limit: int = DEFAULT_BATCH_LIMIT,
    context_loader: ContextLoader | None = None,
) -> list[OutreachMessage]:
    """Instantiate every due, eligible client currently matching this
    template's profile. A client already touched for their next step is
    skipped. A client whose guardrail re-check failed is skipped too, with
    no message and no retry.

    Committed per member rather than once at the end, and an unexpected
    error from one member (a bad PII vault row, a DB blip) is caught and
    logged rather than left to propagate.
    """
    if template.status != "approved":
        raise TemplateNotApproved(template.template_id)

    buckets = derive_buckets(session, campaign_id, limit=limit, context_loader=context_loader)
    bucket = next((b for b in buckets if b.profile_key.as_dict() == template.profile_key), None)
    if bucket is None:
        return []

    messages: list[OutreachMessage] = []
    for member in bucket.members:
        step_no = member.enrollment.current_step + 1
        try:
            touch = record_touch(session, member.enrollment, step_no)
            if touch.message_id is not None:
                session.commit()
                continue

            message = instantiate_message(
                session, template, member.enrollment.client_id, campaign_id=campaign_id
            )
            if message is not None:
                touch.message_id = message.message_id
                session.flush()
                messages.append(message)
        except Exception:
            session.rollback()
            logger.exception(
                "instantiate_template.member_failed",
                campaign_id=campaign_id,
                template_id=template.template_id,
                client_id=member.enrollment.client_id,
            )
            continue
        session.commit()
    return messages
