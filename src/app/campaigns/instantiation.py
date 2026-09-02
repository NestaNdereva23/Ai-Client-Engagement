"""Instantiation: turn an approved template into one outreach_message per
matching client.

A template must be approved before any instance is written from it --
services.review.instantiate_message enforces that. Membership is
re-derived, not replayed from draft time: message_template stores only the
profile, not which enrollments were in it, so a client who becomes due for
the same profile later is picked up too.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog
from sqlalchemy.orm import Session

from app.agents.graph import ContextLoader, load_client_profile_context
from app.campaigns.bucketing import Bucket, derive_buckets
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.touch import record_touch
from app.db.models.message_template import MessageTemplate
from app.db.models.outreach import OutreachMessage
from app.db.session import restricted_session
from app.services.review import (
    TemplateNotApproved,
    instantiate_message_for_template,
    load_template_angle_brief,
    load_template_tier,
)

logger = structlog.get_logger(__name__)


def _matching_bucket(buckets: Sequence[Bucket], template: MessageTemplate) -> Bucket | None:
    return next((b for b in buckets if b.profile_key.as_dict() == template.profile_key), None)


def _instantiate_from_bucket(
    session: Session,
    template: MessageTemplate,
    bucket: Bucket | None,
    *,
    campaign_id: int,
) -> list[OutreachMessage]:
    """Committed per member rather than once at the end, and an unexpected
    error from one member (a bad PII vault row, a DB blip) is caught and
    logged rather than left to propagate.
    """
    if template.status != "approved":
        raise TemplateNotApproved(template.template_id)
    if bucket is None or not bucket.members:
        return []

    tier = load_template_tier(session, template)
    brief = load_template_angle_brief(session, template)

    messages: list[OutreachMessage] = []
    with restricted_session() as vault_session:
        for member in bucket.members:
            step_no = member.enrollment.current_step + 1
            try:
                touch = record_touch(session, member.enrollment, step_no)
                if touch.message_id is not None:
                    session.commit()
                    continue

                message = instantiate_message_for_template(
                    session,
                    template,
                    member.enrollment.client_id,
                    tier=tier,
                    brief=brief,
                    campaign_id=campaign_id,
                    vault_session=vault_session,
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
    """
    if template.status != "approved":
        raise TemplateNotApproved(template.template_id)

    context_loader = context_loader or functools.partial(load_client_profile_context, session)
    buckets = derive_buckets(session, campaign_id, limit=limit, context_loader=context_loader)
    bucket = _matching_bucket(buckets, template)
    return _instantiate_from_bucket(session, template, bucket, campaign_id=campaign_id)


@dataclass(frozen=True)
class InstantiateManyResult:
    instantiated_count: int = 0
    failed_template_ids: list[str] = field(default_factory=list)


def instantiate_many_templates(
    session: Session,
    template_ids: Sequence[str],
    *,
    campaign_id: int,
    limit: int = DEFAULT_BATCH_LIMIT,
    context_loader: ContextLoader | None = None,
) -> InstantiateManyResult:
    context_loader = context_loader or functools.partial(load_client_profile_context, session)
    buckets = derive_buckets(session, campaign_id, limit=limit, context_loader=context_loader)

    instantiated_count = 0
    failed_template_ids: list[str] = []
    for template_id in template_ids:
        template = session.get(MessageTemplate, template_id)
        if template is None:
            failed_template_ids.append(template_id)
            continue
        try:
            bucket = _matching_bucket(buckets, template)
            messages = _instantiate_from_bucket(session, template, bucket, campaign_id=campaign_id)
        except Exception:
            session.rollback()
            logger.exception(
                "instantiate_many_templates.template_failed",
                campaign_id=campaign_id,
                template_id=template_id,
            )
            failed_template_ids.append(template_id)
            continue
        instantiated_count += len(messages)
    return InstantiateManyResult(
        instantiated_count=instantiated_count, failed_template_ids=failed_template_ids
    )
