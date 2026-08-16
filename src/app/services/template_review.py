"""The template review workflow: the queue, and reviewer decisions.

Template review is mandatory and unconditional for every template, every
tier, always -- review_sample_rate and tier_sampling_enabled only ever
govern an instantiated outreach_message's own, separate review_action.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.message_template import (
    TEMPLATE_REVIEW_OUTCOMES,
    MessageTemplate,
    TemplateReviewAction,
)
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_cursor, encode_cursor

logger = structlog.get_logger(__name__)

_TERMINAL_STATUSES = ("approved", "rejected")
_STATUS_FOR_OUTCOME = {
    "approve": "approved",
    "edit_approve": "approved",
    "reject": "rejected",
    "escalate": "escalated",
    "hold": "held",
}


class TemplateNotFound(Exception):
    """No message_template exists with the given id."""


class TemplateAlreadyDecided(Exception):
    """The template is already approved or rejected; deciding again is refused."""


class EditedContentRequired(Exception):
    """edit_approve was chosen without the reviewer's edited content."""


class InvalidOutcome(Exception):
    """outcome is not one of the five allowed review outcomes."""


@dataclass(frozen=True)
class BatchDecideTemplateFailure:
    """One template a decide_template_batch call could not decide, and why."""

    template_id: str
    error: str


@dataclass(frozen=True)
class BatchDecideTemplateResult:
    """What one decide_template_batch call did: an action per template that
    decided cleanly, and a reason per template that didn't. A failure never
    rolls back the ones that succeeded.
    """

    decided: list[TemplateReviewAction] = field(default_factory=list)
    failed: list[BatchDecideTemplateFailure] = field(default_factory=list)


def list_pending_templates(
    session: Session,
    *,
    status: str = "pending_review",
    campaign_id: int | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[MessageTemplate], str | None]:
    """One page of templates in the given status, oldest first."""
    limit = clamp_limit(limit)
    query = select(MessageTemplate).where(MessageTemplate.status == status)
    if campaign_id is not None:
        query = query.where(MessageTemplate.campaign_id == campaign_id)
    if cursor is not None:
        after_created_at, after_id = decode_cursor(cursor)
        query = query.where(
            tuple_(MessageTemplate.created_at, MessageTemplate.template_id)
            > (after_created_at, after_id)
        )
    query = query.order_by(MessageTemplate.created_at, MessageTemplate.template_id).limit(limit + 1)
    rows = list(session.scalars(query).all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.template_id)
    return rows, next_cursor


def get_template(session: Session, template_id: str) -> MessageTemplate:
    """One template, or raise TemplateNotFound."""
    template = session.get(MessageTemplate, template_id)
    if template is None:
        raise TemplateNotFound(template_id)
    return template


def get_template_review_history(session: Session, template_id: str) -> list[TemplateReviewAction]:
    """Every decision made on one template, oldest first."""
    return list(
        session.scalars(
            select(TemplateReviewAction)
            .where(TemplateReviewAction.template_id == template_id)
            .order_by(TemplateReviewAction.created_at)
        ).all()
    )


def _diff_lines(before: str, after: str) -> list[str]:
    return list(
        difflib.unified_diff(str(before).splitlines(), str(after).splitlines(), lineterm="")
    )


def compute_edit_diff(ai_draft_content: dict, edited_content: dict) -> dict[str, list[str]]:
    """A per-field unified diff between what the model wrote and what the
    reviewer changed it to."""
    diff: dict[str, list[str]] = {}
    for field_name, after in edited_content.items():
        before = ai_draft_content.get(field_name, "")
        if before != after:
            diff[field_name] = _diff_lines(before, after)
    return diff


def decide_template(
    session: Session,
    template_id: str,
    *,
    outcome: str,
    reviewer_id: str,
    reason: str | None = None,
    edited_content: dict | None = None,
) -> TemplateReviewAction:
    """Record a reviewer's decision and move the template to the resulting status.

    edit_approve overwrites ai_draft_content with the reviewer's edited
    version -- every future instantiation reads it from there, so the diff
    recorded here is the last time the model's own wording is visible
    anywhere.
    """
    if outcome not in TEMPLATE_REVIEW_OUTCOMES:
        raise InvalidOutcome(outcome)
    if outcome == "edit_approve" and not edited_content:
        raise EditedContentRequired("edit_approve requires edited_content")

    template = get_template(session, template_id)
    if template.status in _TERMINAL_STATUSES:
        raise TemplateAlreadyDecided(f"{template_id} is already {template.status}")

    profile_key = template.profile_key or {}
    edit_diff = (
        compute_edit_diff(template.ai_draft_content, edited_content)
        if outcome == "edit_approve"
        else None
    )

    action = TemplateReviewAction(
        template_id=template_id,
        reviewer_id=reviewer_id,
        outcome=outcome,
        edited_content=edited_content,
        message_angle=profile_key.get("message_angle"),
        priority_tier=profile_key.get("priority_tier"),
        edit_diff=edit_diff,
        reason=reason,
    )
    session.add(action)
    template.status = _STATUS_FOR_OUTCOME[outcome]
    if outcome == "edit_approve":
        template.ai_draft_content = edited_content
    record_audit(
        session,
        entity_type="message_template_review_action",
        action=outcome,
        entity_id=template_id,
        actor_id=reviewer_id,
        detail={"outcome": outcome, "reason": reason},
    )
    session.flush()
    return action


def decide_template_batch(
    session: Session,
    template_ids: list[str],
    *,
    outcome: str,
    reviewer_id: str,
    reason: str | None = None,
) -> BatchDecideTemplateResult:
    """Apply decide_template() to each template in template_ids, one at a time.

    Same template_review_action and audit row per template as calling POST
    .../decide once per id -- this just saves the round trips. A template
    that can't be decided (not found, already decided) is skipped and
    reported in .failed rather than aborting the rest of the batch.

    edit_approve is refused up front: a batch shares one outcome and
    reason across every template, and an edit is inherently per-template,
    so there is no sensible edited_content to apply to all of them.
    """
    if outcome not in TEMPLATE_REVIEW_OUTCOMES:
        raise InvalidOutcome(outcome)
    if outcome == "edit_approve":
        raise EditedContentRequired(
            "edit_approve is not supported by decide_template_batch; edit templates one at a time"
        )

    decided: list[TemplateReviewAction] = []
    failed: list[BatchDecideTemplateFailure] = []
    for template_id in template_ids:
        try:
            action = decide_template(
                session, template_id, outcome=outcome, reviewer_id=reviewer_id, reason=reason
            )
        except TemplateNotFound:
            failed.append(BatchDecideTemplateFailure(template_id=template_id, error="not_found"))
        except TemplateAlreadyDecided as exc:
            failed.append(BatchDecideTemplateFailure(template_id=template_id, error=str(exc)))
        else:
            decided.append(action)
    return BatchDecideTemplateResult(decided=decided, failed=failed)
