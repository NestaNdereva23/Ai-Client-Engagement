"""The review workflow: re-attachment, the queue, and reviewer decisions.

Re-attachment runs entirely after generation and guardrails; nothing here
calls the model, so the resolved values are never seen by it. The only read
of pii_vault happens under the restricted role, scoped to that one lookup,
in its own session so the rest of this module never even holds a connection
with a grant on the vault.

decide() is the one place a review_action gets written and an
outreach_message's status changes; escalate and hold are waypoints, not
endpoints, so a message stays decidable after either, while approve and
reject are terminal. Every review_action also carries the angle and tier its
draft was generated under, and edit_approve additionally carries a per-field
diff: together these are the ground-truth label a judge-agreement analysis
joins against.
"""

from __future__ import annotations

import difflib
import uuid

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.llmops import GenerationRun, PromptVersion
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import REVIEW_OUTCOMES, OutreachMessage, ReviewAction
from app.db.session import restricted_session
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_cursor, encode_cursor

logger = structlog.get_logger(__name__)

_FALLBACK_FIRST_NAME = "Valued Client"
_FALLBACK_FUND_NAME = "your fund"

_TERMINAL_STATUSES = ("approved", "rejected")
_STATUS_FOR_OUTCOME = {
    "approve": "approved",
    "edit_approve": "approved",
    "reject": "rejected",
    "escalate": "escalated",
    "hold": "held",
}


class MessageNotFound(Exception):
    """No outreach_message exists with the given id."""


class MessageAlreadyDecided(Exception):
    """The message is already approved or rejected; deciding again is refused."""


class EditedContentRequired(Exception):
    """edit_approve was chosen without the reviewer's edited content."""


class InvalidOutcome(Exception):
    """outcome is not one of the five allowed review outcomes."""


def _first_name_from_full_name(full_name: str | None) -> str:
    """The greeting name from a stored full name, or a safe fallback when
    there is nothing on file; a real client should never receive a blank or
    malformed greeting.
    """
    if not full_name or not full_name.strip():
        return _FALLBACK_FIRST_NAME
    return full_name.strip().split()[0]


def _fetch_client_name(client_id: int) -> str | None:
    """The vault's raw client_name for one client, read under the restricted role."""
    with restricted_session() as session:
        vault = session.get(PiiVault, client_id)
        record_audit(
            session,
            entity_type="pii_vault",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "personalization_inject"},
        )
        session.commit()
        return vault.client_name if vault else None


def resolve_placeholders(
    text: str,
    *,
    first_name: str,
    fund_name: str,
    typical_contribution: str | None = None,
    largest_contribution: str | None = None,
    years_since_exit: str | None = None,
    days_held_after_last_topup: str | None = None,
    month_they_left: str | None = None,
) -> str:
    """Substitute every placeholder this draft has a value for.

    first_name and fund_name are the two every draft has always needed, so
    they stay required. The rest are set only once a bucketed template
    references them (ST5), and are skipped rather than blanked out when
    absent, since a draft that never used e.g. {{years_since_exit}} needs
    nothing supplied for it.
    """
    resolved = text.replace("{{first_name}}", first_name).replace("{{fund_name}}", fund_name)
    placeholder_facts = {
        "typical_contribution": typical_contribution,
        "largest_contribution": largest_contribution,
        "years_since_exit": years_since_exit,
        "days_held_after_last_topup": days_held_after_last_topup,
        "month_they_left": month_they_left,
    }
    for field, value in placeholder_facts.items():
        if value is not None:
            resolved = resolved.replace(f"{{{{{field}}}}}", str(value))
    return resolved


def personalize_content(
    ai_draft_content: dict,
    *,
    first_name: str,
    fund_name: str,
    typical_contribution: str | None = None,
    largest_contribution: str | None = None,
    years_since_exit: str | None = None,
    days_held_after_last_topup: str | None = None,
    month_they_left: str | None = None,
) -> dict:
    """The subject/body pair with real values injected in place of placeholders."""
    kwargs = {
        "first_name": first_name,
        "fund_name": fund_name,
        "typical_contribution": typical_contribution,
        "largest_contribution": largest_contribution,
        "years_since_exit": years_since_exit,
        "days_held_after_last_topup": days_held_after_last_topup,
        "month_they_left": month_they_left,
    }
    return {
        "subject": resolve_placeholders(ai_draft_content["subject"], **kwargs),
        "body": resolve_placeholders(ai_draft_content["body"], **kwargs),
    }


def create_outreach_message(
    session: Session, run: GenerationRun, *, campaign_id: int
) -> OutreachMessage:
    """Re-attach real values to an accepted run and store the result.

    ai_draft_content is copied from the run unchanged; personalized_content
    is computed fresh here. run must be accepted (has ai_draft_content); a
    run with nothing else to review has no message to create.
    """
    client = session.get(Clients, run.client_id)
    fund = session.get(Funds, client.unit_fund_id) if client else None
    fund_name = fund.unit_fund_name if fund else _FALLBACK_FUND_NAME

    full_name = _fetch_client_name(run.client_id)
    first_name = _first_name_from_full_name(full_name)
    if full_name is None:
        logger.warning("personalization.no_client_name", client_id=run.client_id)

    personalized = personalize_content(
        run.ai_draft_content, first_name=first_name, fund_name=fund_name
    )

    message = OutreachMessage(
        message_id=uuid.uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        client_id=run.client_id,
        ai_draft_content=run.ai_draft_content,
        personalized_content=personalized,
    )
    session.add(message)
    record_audit(
        session,
        entity_type="outreach_message",
        action="create",
        entity_id=message.message_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
    )
    session.flush()
    return message


def list_pending_messages(
    session: Session,
    *,
    status: str = "pending_review",
    campaign_id: int | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[OutreachMessage], str | None]:
    """One page of messages in the given status, oldest first: the reviewer's queue.

    Ordered by (created_at, message_id) rather than created_at alone, so two
    messages created in the same instant still sort deterministically and a
    cursor built from one of them is unambiguous.
    """
    limit = clamp_limit(limit)
    query = select(OutreachMessage).where(OutreachMessage.status == status)
    if campaign_id is not None:
        query = query.where(OutreachMessage.campaign_id == campaign_id)
    if cursor is not None:
        after_created_at, after_id = decode_cursor(cursor)
        query = query.where(
            tuple_(OutreachMessage.created_at, OutreachMessage.message_id)
            > (after_created_at, after_id)
        )
    query = query.order_by(OutreachMessage.created_at, OutreachMessage.message_id).limit(limit + 1)
    rows = list(session.scalars(query).all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.message_id)
    return rows, next_cursor


def get_message(session: Session, message_id: str) -> OutreachMessage:
    """One message, or raise MessageNotFound."""
    message = session.get(OutreachMessage, message_id)
    if message is None:
        raise MessageNotFound(message_id)
    return message


def _label_for_message(session: Session, message: OutreachMessage) -> tuple[str | None, str | None]:
    """The (angle, tier) this message's draft was actually generated under.

    Read from the generation run rather than the client's current
    client_message_indicators row, which is upserted per client and may have
    since resolved to a different angle or tier: the label has to describe
    the draft being reviewed, not whatever the client looks like today.
    """
    run = session.get(GenerationRun, message.generation_run_id)
    if run is None:
        return None, None
    prompt_version = session.get(PromptVersion, run.prompt_version_id)
    angle = prompt_version.angle if prompt_version else None
    return angle, run.priority_tier


def _diff_lines(before: str, after: str) -> list[str]:
    return list(
        difflib.unified_diff(str(before).splitlines(), str(after).splitlines(), lineterm="")
    )


def compute_edit_diff(ai_draft_content: dict, edited_content: dict) -> dict[str, list[str]]:
    """A per-field unified diff between what the model wrote and what the
    reviewer changed it to; only fields the reviewer actually touched appear,
    so an edit that only ever changed the subject shows no body diff.
    """
    diff: dict[str, list[str]] = {}
    for field, after in edited_content.items():
        before = ai_draft_content.get(field, "")
        if before != after:
            diff[field] = _diff_lines(before, after)
    return diff


def get_review_history(session: Session, message_id: str) -> list[ReviewAction]:
    """Every decision made on one message, oldest first."""
    return list(
        session.scalars(
            select(ReviewAction)
            .where(ReviewAction.message_id == message_id)
            .order_by(ReviewAction.created_at)
        ).all()
    )


def decide(
    session: Session,
    message_id: str,
    *,
    outcome: str,
    reviewer_id: str,
    reason: str | None = None,
    edited_content: dict | None = None,
) -> ReviewAction:
    """Record a reviewer's decision and move the message to the resulting status."""
    if outcome not in REVIEW_OUTCOMES:
        raise InvalidOutcome(outcome)
    if outcome == "edit_approve" and not edited_content:
        raise EditedContentRequired("edit_approve requires edited_content")

    message = get_message(session, message_id)
    if message.status in _TERMINAL_STATUSES:
        raise MessageAlreadyDecided(f"{message_id} is already {message.status}")

    message_angle, priority_tier = _label_for_message(session, message)
    edit_diff = (
        compute_edit_diff(message.ai_draft_content, edited_content)
        if outcome == "edit_approve"
        else None
    )

    action = ReviewAction(
        message_id=message_id,
        reviewer_id=reviewer_id,
        outcome=outcome,
        edited_content=edited_content,
        message_angle=message_angle,
        priority_tier=priority_tier,
        edit_diff=edit_diff,
        reason=reason,
    )
    session.add(action)
    message.status = _STATUS_FOR_OUTCOME[outcome]
    record_audit(
        session,
        entity_type="review_action",
        action=outcome,
        entity_id=message_id,
        actor_id=reviewer_id,
        detail={"outcome": outcome, "reason": reason},
    )
    session.flush()
    return action
