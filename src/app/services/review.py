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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.agents.email_agent import render_call_brief
from app.agents.email_channel import CALL_BRIEF_CHANNEL
from app.agents.graph import load_client_facts
from app.agents.guardrails import (
    GuardrailFailure,
    check_no_unresolved_placeholders,
    instance_numeric_traceability_check,
)
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.llmops import GenerationRun, PromptVersion
from app.db.models.message_template import MessageTemplate
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import REVIEW_OUTCOMES, OutreachMessage, ReviewAction
from app.db.models.rules import TierContract
from app.db.models.views import llm_client_context
from app.db.session import restricted_session
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_cursor, encode_cursor
from app.rules.catalog import load_angle
from app.rules.tier_contract import instance_needs_review, load_tier

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


class TemplateNotApproved(Exception):
    """Instantiation was attempted against a message_template that is not approved."""


@dataclass(frozen=True)
class BatchDecideFailure:
    """One message a decide_batch call could not decide, and why."""

    message_id: str
    error: str


@dataclass(frozen=True)
class BatchDecideResult:
    """What one decide_batch call did: an action per message that decided
    cleanly, and a reason per message that didn't. A failure never rolls
    back the ones that succeeded.
    """

    decided: list[ReviewAction] = field(default_factory=list)
    failed: list[BatchDecideFailure] = field(default_factory=list)


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
    cadence_interval_days: str | None = None,
) -> str:
    """Substitute every placeholder this draft has a value for.

    first_name and fund_name stay required. The rest are skipped rather
    than blanked out when absent, since a draft that never used one needs
    nothing supplied for it.
    """
    resolved = text.replace("{{first_name}}", first_name).replace("{{fund_name}}", fund_name)
    placeholder_facts = {
        "typical_contribution": typical_contribution,
        "largest_contribution": largest_contribution,
        "years_since_exit": years_since_exit,
        "days_held_after_last_topup": days_held_after_last_topup,
        "month_they_left": month_they_left,
        "cadence_interval_days": cadence_interval_days,
    }
    for field_name, value in placeholder_facts.items():
        if value is not None:
            resolved = resolved.replace(f"{{{{{field_name}}}}}", str(value))
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
    cadence_interval_days: str | None = None,
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
        "cadence_interval_days": cadence_interval_days,
    }
    return {
        "subject": resolve_placeholders(ai_draft_content["subject"], **kwargs),
        "body": resolve_placeholders(ai_draft_content["body"], **kwargs),
    }


def _resolve_fund_name(session: Session, client_id: int) -> str:
    """This client's real fund name, read after the model boundary -- safe
    to use directly, unlike the restricted display-name vocabulary a draft
    is allowed to cite."""
    client = session.get(Clients, client_id)
    fund = session.get(Funds, client.unit_fund_id) if client else None
    return fund.unit_fund_name if fund else _FALLBACK_FUND_NAME


def _resolve_first_name(client_id: int) -> str:
    full_name = _fetch_client_name(client_id)
    first_name = _first_name_from_full_name(full_name)
    if full_name is None:
        logger.warning("personalization.no_client_name", client_id=client_id)
    return first_name


def create_outreach_message(
    session: Session, run: GenerationRun, *, campaign_id: int, call_brief: str | None = None
) -> OutreachMessage:
    """Re-attach real values to an accepted run and store the result.

    ai_draft_content is copied from the run unchanged; personalized_content
    is computed fresh here. call_brief, when given, is stored as-is -- it
    carries no placeholder, so there is nothing to personalize.
    """
    fund_name = _resolve_fund_name(session, run.client_id)
    first_name = _resolve_first_name(run.client_id)

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
        call_brief=call_brief,
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


def _format_amount(value: int | float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _format_month(value: str) -> str:
    """ "YYYY-MM" -> "March 2025", the way a real email would read it."""
    return datetime.strptime(value, "%Y-%m").strftime("%B %Y")


def _placeholder_facts_for_client(
    session: Session, client_id: int
) -> tuple[dict[str, Any], dict[str, str]]:
    """This client's raw fact values, and the formatted strings
    personalize_content substitutes them with. A client with no numeric row
    yet gets nothing to substitute.
    """
    row = (
        session.execute(
            select(llm_client_context).where(llm_client_context.c.client_id == client_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return {}, {}
    raw = load_client_facts(session, client_id, dict(row)) or {}

    formatted: dict[str, str] = {}
    if raw.get("typical_contribution_kes") is not None:
        formatted["typical_contribution"] = _format_amount(raw["typical_contribution_kes"])
    if raw.get("largest_contribution_kes") is not None:
        formatted["largest_contribution"] = _format_amount(raw["largest_contribution_kes"])
    if raw.get("years_since_exit") is not None:
        formatted["years_since_exit"] = f"{raw['years_since_exit']:g}"
    if raw.get("days_held_after_last_topup") is not None:
        formatted["days_held_after_last_topup"] = str(raw["days_held_after_last_topup"])
    if raw.get("month_they_left") is not None:
        formatted["month_they_left"] = _format_month(raw["month_they_left"])
    if raw.get("invested_every_n_days") is not None:
        formatted["cadence_interval_days"] = str(raw["invested_every_n_days"])
    return raw, formatted


def _load_template_tier(session: Session, template: MessageTemplate) -> TierContract | None:
    priority_tier = (template.profile_key or {}).get("priority_tier")
    return load_tier(session, priority_tier, date.today()) if priority_tier else None


def _instance_status(tier: TierContract | None) -> str:
    """pending_review, unless this tier's sampling policy says this instance
    does not need a human look. Never touches whether the template itself
    was reviewed -- that is mandatory and already enforced elsewhere.
    """
    settings = get_settings()
    needs_review = instance_needs_review(tier, sampling_enabled=settings.tier_sampling_enabled)
    return "pending_review" if needs_review else "approved"


def _call_brief_for_instance(
    session: Session,
    template: MessageTemplate,
    tier: TierContract | None,
    client_facts: Mapping[str, Any],
) -> str | None:
    """This client's own call brief, when the tier's contract calls for one.
    Built from the angle's own brief text and this client's real facts,
    the same way an individual client's own draft builds it. A template
    never renders one at draft time -- only here, per client.
    """
    if tier is None or tier.secondary_channel != CALL_BRIEF_CHANNEL:
        return None
    angle = (template.profile_key or {}).get("message_angle")
    if not angle:
        return None
    brief = load_angle(session, angle, date.today())
    if brief is None:
        return None
    return render_call_brief(brief=brief, facts=client_facts, contract=tier)


def instantiate_message(
    session: Session, template: MessageTemplate, client_id: int, *, campaign_id: int
) -> OutreachMessage | None:
    """Approved template + one client's own facts -> an outreach_message, or
    None if the post-instantiation guardrail re-check fails.

    Raises TemplateNotApproved if the template itself has not been
    approved. A guardrail failure is logged and returns None -- no message,
    no automatic retry.
    """
    if template.status != "approved":
        raise TemplateNotApproved(template.template_id)

    tier = _load_template_tier(session, template)
    fund_name = _resolve_fund_name(session, client_id)
    first_name = _resolve_first_name(client_id)
    client_raw_facts, placeholder_kwargs = _placeholder_facts_for_client(session, client_id)

    personalized = personalize_content(
        template.ai_draft_content,
        first_name=first_name,
        fund_name=fund_name,
        **placeholder_kwargs,
    )

    try:
        check_no_unresolved_placeholders(personalized["subject"], personalized["body"])
        instance_numeric_traceability_check(
            template_body=template.ai_draft_content.get("body", ""),
            resolved_body=personalized["body"],
            client_facts=client_raw_facts,
        )
    except GuardrailFailure as failure:
        record_audit(
            session,
            entity_type="message_template",
            action="instantiation_rejected",
            entity_id=template.template_id,
            detail={
                "client_id": client_id,
                "guardrail": failure.guardrail,
                "reason": str(failure),
            },
        )
        return None

    message = OutreachMessage(
        message_id=uuid.uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=template.generation_run_id,
        template_id=template.template_id,
        client_id=client_id,
        ai_draft_content=template.ai_draft_content,
        personalized_content=personalized,
        call_brief=_call_brief_for_instance(session, template, tier, client_raw_facts),
        status=_instance_status(tier),
    )
    session.add(message)
    record_audit(
        session,
        entity_type="outreach_message",
        action="create",
        entity_id=message.message_id,
        run_id=template.generation_run_id,
        detail={"template_id": template.template_id, "status": message.status},
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
    for field_name, after in edited_content.items():
        before = ai_draft_content.get(field_name, "")
        if before != after:
            diff[field_name] = _diff_lines(before, after)
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


def decide_batch(
    session: Session,
    message_ids: list[str],
    *,
    outcome: str,
    reviewer_id: str,
    reason: str | None = None,
) -> BatchDecideResult:
    """Apply decide() to each message in message_ids, one at a time.

    Same review_action and audit row per message as calling POST
    .../decide once per id -- this just saves the round trips. A message
    that can't be decided (not found, already decided) is skipped and
    reported in .failed rather than aborting the rest of the batch.

    edit_approve is refused up front: a batch shares one outcome and
    reason across every message, and an edit is inherently per-message,
    so there is no sensible edited_content to apply to all of them.
    """
    if outcome not in REVIEW_OUTCOMES:
        raise InvalidOutcome(outcome)
    if outcome == "edit_approve":
        raise EditedContentRequired(
            "edit_approve is not supported by decide_batch; edit messages one at a time"
        )

    decided: list[ReviewAction] = []
    failed: list[BatchDecideFailure] = []
    for message_id in message_ids:
        try:
            action = decide(
                session, message_id, outcome=outcome, reviewer_id=reviewer_id, reason=reason
            )
        except MessageNotFound:
            failed.append(BatchDecideFailure(message_id=message_id, error="not_found"))
        except MessageAlreadyDecided as exc:
            failed.append(BatchDecideFailure(message_id=message_id, error=str(exc)))
        else:
            decided.append(action)
    return BatchDecideResult(decided=decided, failed=failed)
