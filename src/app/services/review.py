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
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import case, func, or_, select, tuple_
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
from app.campaigns.cohorts import CohortSlot, resolve_cohort_slot
from app.config import get_settings
from app.db.models.llmops import Evaluation, GenerationRun, PromptVersion
from app.db.models.message_template import MessageTemplate
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import REVIEW_OUTCOMES, OutreachMessage, ReviewAction, ReviewCohort
from app.db.models.rules import MessageAngleCatalog, TierContract
from app.db.models.views import llm_client_context
from app.db.session import restricted_session
from app.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor,
    decode_ranked_cursor,
    encode_cursor,
    encode_ranked_cursor,
)
from app.privacy.fact_block import round_sig_figs
from app.rules.catalog import load_angle
from app.rules.tier_contract import instance_needs_review, load_tier

# Display order for the reviewer queue: T1 is the highest-value, always-
# reviewed tier, so it goes first. Not the same thing as app.rules.store's
# PRIORITY_TIERS, which is just the set of valid tiers with no order.
_QUEUE_TIER_ORDER = ("T1", "T2", "T3", "T4")


def _tier_rank(tier: str | None) -> int:
    """T1 -> 0 ... T4 -> 3; anything else (no run, unknown tier) sorts last."""
    return _QUEUE_TIER_ORDER.index(tier) if tier in _QUEUE_TIER_ORDER else len(_QUEUE_TIER_ORDER)


# Outcomes never allowed on a message that belongs to a sampling cohort --
# a cohort message only ever gets approved (as-is or edited), the same
# restriction the review queue UI enforces by not offering the buttons.
_COHORT_DISALLOWED_OUTCOMES = ("reject", "escalate", "hold")

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


class CohortNotFound(Exception):
    """No review_cohort exists with the given id."""


class CohortNotReady(Exception):
    """approve_cohort_remainder was called before every sample was decided."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


@dataclass(frozen=True)
class CohortReadyInfo:
    """A cohort whose last sample was just decided, and how many other
    messages in it are still waiting on the bulk approval this unlocks.
    """

    cohort_id: str
    remaining_count: int


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
    session: Session,
    run: GenerationRun,
    *,
    campaign_id: int,
    call_brief: str | None = None,
    cohort_slot: CohortSlot | None = None,
) -> OutreachMessage:
    """Re-attach real values to an accepted run and store the result.

    ai_draft_content is copied from the run unchanged; personalized_content
    is computed fresh here. call_brief, when given, is stored as-is -- it
    carries no placeholder, so there is nothing to personalize.

    cohort_slot decides whether this single-generated message belongs to a
    review sampling cohort, and whether it's one of the cohort's samples.
    Left unset (the normal case), it's resolved automatically from the
    run's own priority_tier, claiming a fresh slot in that campaign x
    tier's cohort. A caller replacing one message with another for the
    same client (regenerate_message) passes the old message's slot through
    explicitly instead, so a regenerate doesn't consume a second slot.
    """
    fund_name = _resolve_fund_name(session, run.client_id)
    first_name = _resolve_first_name(run.client_id)

    personalized = personalize_content(
        run.ai_draft_content, first_name=first_name, fund_name=fund_name
    )

    if cohort_slot is None:
        cohort_slot = resolve_cohort_slot(
            session, campaign_id=campaign_id, priority_tier=run.priority_tier
        )

    message = OutreachMessage(
        message_id=uuid.uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        client_id=run.client_id,
        ai_draft_content=run.ai_draft_content,
        personalized_content=personalized,
        call_brief=call_brief,
        cohort_id=cohort_slot.cohort_id,
        is_sample=cohort_slot.is_sample,
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


# The only two raw facts ModelFactBlock rounds (app.privacy.fact_block);
# every other fact block field is already a band or a coarsened date, with
# nothing exact behind it worth showing a reviewer twice.
_ROUNDED_FACT_FIELDS = ("typical_contribution_kes", "largest_contribution_kes")


def client_fact_pairs(session: Session, client_id: int) -> dict[str, tuple[Any, Any]]:
    """This client's exact figure next to the rounded one the model actually
    saw, for each amount ModelFactBlock rounds -- so a reviewer can catch a
    rounding that reads wrong for this client. Recomputes the rounding
    fresh from today's llm_client_numeric_facts row rather than reading
    back what a past run saw, so it can drift from the run's own figure if
    the client's numbers have moved on since (the same gap V5 logged and
    left as a follow-up, not solved here).

    Keyed by field name, each value (exact, rounded). A client with no
    numeric row yet, or a field that was never on file, is left out.
    """
    raw, _formatted = _placeholder_facts_for_client(session, client_id)
    return {
        field_name: (raw[field_name], round_sig_figs(raw[field_name]))
        for field_name in _ROUNDED_FACT_FIELDS
        if raw.get(field_name) is not None
    }


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


def _pending_messages_filters(
    *, status: str, campaign_id: int | None, only_sampled: bool
) -> list[Any]:
    """The where-clauses list_pending_messages and count_pending_messages
    both filter on -- kept in one place so a count can never drift from
    what the page beside it actually shows.
    """
    clauses: list[Any] = [OutreachMessage.status == status]
    if campaign_id is not None:
        clauses.append(OutreachMessage.campaign_id == campaign_id)
    if only_sampled:
        clauses.append(
            or_(OutreachMessage.cohort_id.is_(None), OutreachMessage.is_sample.is_(True))
        )
    return clauses


def list_pending_messages(
    session: Session,
    *,
    status: str = "pending_review",
    campaign_id: int | None = None,
    only_sampled: bool = True,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[OutreachMessage], str | None]:
    """One page of messages in the given status, oldest first: the reviewer's queue.

    Ordered by (created_at, message_id) rather than created_at alone, so two
    messages created in the same instant still sort deterministically and a
    cursor built from one of them is unambiguous.

    only_sampled (default True) hides a cohort's non-sample messages -- the
    ones riding on their cohort's sample outcome rather than being
    individually surfaced. A message with no cohort at all (template
    instances, anything predating cohort sampling) is unaffected either
    way. Pass False to see everything, e.g. a campaign manager checking a
    cohort's non-sample messages before they're auto-approved.
    """
    limit = clamp_limit(limit)
    filters = _pending_messages_filters(
        status=status, campaign_id=campaign_id, only_sampled=only_sampled
    )
    query = select(OutreachMessage).where(*filters)
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


def list_pending_messages_for_queue(
    session: Session,
    *,
    campaign_id: int | None = None,
    only_sampled: bool = True,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[tuple[OutreachMessage, str | None]], str | None]:
    """One page of the pending queue, T1 first through T4, oldest first
    within a tier -- the order the reviewer console shows it in, since T1
    is mandatory review and the highest value. A message whose run carries
    no recognised tier sorts last, after every named one.

    Same filters as list_pending_messages; the ordering (and so the cursor
    shape) differs to make room for tier ahead of created_at, and each row
    carries its tier alongside the message, both read off the same join,
    so the queue can label a row without a second query per message.
    """
    limit = clamp_limit(limit)
    filters = _pending_messages_filters(
        status="pending_review", campaign_id=campaign_id, only_sampled=only_sampled
    )
    tier_rank = case(
        {tier: rank for rank, tier in enumerate(_QUEUE_TIER_ORDER)},
        value=GenerationRun.priority_tier,
        else_=len(_QUEUE_TIER_ORDER),
    )
    query = (
        select(OutreachMessage, GenerationRun.priority_tier)
        .join(GenerationRun, OutreachMessage.generation_run_id == GenerationRun.run_id)
        .where(*filters)
    )
    if cursor is not None:
        after_rank, after_created_at, after_id = decode_ranked_cursor(cursor)
        query = query.where(
            tuple_(tier_rank, OutreachMessage.created_at, OutreachMessage.message_id)
            > (after_rank, after_created_at, after_id)
        )
    query = query.order_by(tier_rank, OutreachMessage.created_at, OutreachMessage.message_id).limit(
        limit + 1
    )
    rows = [tuple(row) for row in session.execute(query).all()]

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last_message, last_tier = rows[-1]
        next_cursor = encode_ranked_cursor(
            _tier_rank(last_tier), last_message.created_at, last_message.message_id
        )
    return rows, next_cursor


def count_pending_messages(
    session: Session,
    *,
    status: str = "pending_review",
    campaign_id: int | None = None,
    only_sampled: bool = True,
) -> int:
    """How many messages list_pending_messages' filters would return in
    total, across every page -- what a queue badge shows.
    """
    return session.scalar(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            *_pending_messages_filters(
                status=status, campaign_id=campaign_id, only_sampled=only_sampled
            )
        )
    )


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


@dataclass(frozen=True)
class MessageReviewContext:
    """Everything the reviewer console shows beside a message's own draft:
    the angle brief and tier contract active when it was generated (not
    whatever is active today), the client's exact-vs-rounded figures, and
    what the generation run and its judge evaluation (if any) say about
    the draft that resulted -- attempts, a failed guardrail's name, and
    the four rubric scores, so a reviewer isn't judging blind on whether
    this draft already needed a retry or already scored badly.
    """

    angle: str | None
    priority_tier: str | None
    angle_spec: MessageAngleCatalog | None
    tier_contract: TierContract | None
    fact_pairs: dict[str, tuple[Any, Any]]
    attempts: int | None
    failed_guardrail: str | None
    evaluation: Evaluation | None


def _latest_evaluation(session: Session, run_id: str) -> Evaluation | None:
    return session.scalar(
        select(Evaluation).where(Evaluation.run_id == run_id).order_by(Evaluation.created_at.desc())
    )


def build_review_context(session: Session, message: OutreachMessage) -> MessageReviewContext:
    """Resolve the angle brief and tier contract as of this message's own
    generation run, so the reviewer judges the draft against the brief
    that actually produced it, not a catalogue that has since moved on.
    """
    angle, priority_tier = _label_for_message(session, message)
    run = session.get(GenerationRun, message.generation_run_id)
    at = run.data_date if run and run.data_date else date.today()
    return MessageReviewContext(
        angle=angle,
        priority_tier=priority_tier,
        angle_spec=load_angle(session, angle, at) if angle else None,
        tier_contract=load_tier(session, priority_tier, at) if priority_tier else None,
        fact_pairs=client_fact_pairs(session, message.client_id),
        attempts=run.attempts if run else None,
        failed_guardrail=run.failed_guardrail if run else None,
        evaluation=_latest_evaluation(session, run.run_id) if run else None,
    )


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
    if message.cohort_id is not None and outcome in _COHORT_DISALLOWED_OUTCOMES:
        raise InvalidOutcome(outcome)

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
        except InvalidOutcome as exc:
            # Only reachable here for a per-message refusal (a cohort
            # message disallowing reject/escalate/hold): outcome itself was
            # already validated against REVIEW_OUTCOMES above, once, for
            # the whole batch.
            failed.append(BatchDecideFailure(message_id=message_id, error=str(exc)))
        else:
            decided.append(action)
    return BatchDecideResult(decided=decided, failed=failed)


def check_cohort_ready(session: Session, message: OutreachMessage) -> CohortReadyInfo | None:
    """Whether the message just decided was its cohort's last pending
    sample, and if so, flip the cohort to ready_to_approve_rest.

    Call after a successful decide() on a message with cohort_id set.
    Returns None for anything that isn't a decided sample, for a cohort
    that isn't still sampling, or while other samples are still pending.
    A cohort with nothing left to bulk-approve (every message was a
    sample) is marked completed here directly rather than left dangling
    in ready_to_approve_rest with nothing for approve_cohort_remainder to
    do.
    """
    if message.cohort_id is None or not message.is_sample:
        return None
    cohort = session.get(ReviewCohort, message.cohort_id)
    if cohort is None or cohort.status != "sampling":
        return None

    pending_samples = session.scalar(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            OutreachMessage.cohort_id == cohort.cohort_id,
            OutreachMessage.is_sample.is_(True),
            OutreachMessage.status == "pending_review",
        )
    )
    if pending_samples:
        return None

    remaining_rest = session.scalar(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            OutreachMessage.cohort_id == cohort.cohort_id,
            OutreachMessage.is_sample.is_(False),
            OutreachMessage.status == "pending_review",
        )
    )
    if not remaining_rest:
        cohort.status = "completed"
        cohort.completed_at = datetime.now(UTC)
        session.flush()
        return None

    cohort.status = "ready_to_approve_rest"
    session.flush()
    return CohortReadyInfo(cohort_id=cohort.cohort_id, remaining_count=remaining_rest)


def approve_cohort_remainder(
    session: Session, cohort_id: str, *, reviewer_id: str
) -> BatchDecideResult:
    """Approve every message riding on a cohort's now-approved sample.

    Refuses (CohortNotReady) unless every sample already has a decision --
    check_cohort_ready is what flips a cohort into that state, so a stray
    early call can't jump the queue. Applies decide_batch the same way any
    other bulk approval does, one review_action and audit row per message,
    then marks the cohort completed.
    """
    cohort = session.get(ReviewCohort, cohort_id)
    if cohort is None:
        raise CohortNotFound(cohort_id)
    if cohort.status != "ready_to_approve_rest":
        raise CohortNotReady(cohort.status)

    message_ids = list(
        session.scalars(
            select(OutreachMessage.message_id).where(
                OutreachMessage.cohort_id == cohort_id,
                OutreachMessage.is_sample.is_(False),
                OutreachMessage.status == "pending_review",
            )
        ).all()
    )
    result = (
        decide_batch(
            session,
            message_ids,
            outcome="approve",
            reviewer_id=reviewer_id,
            reason="cohort sample approved",
        )
        if message_ids
        else BatchDecideResult()
    )
    cohort.status = "completed"
    cohort.completed_at = datetime.now(UTC)
    session.flush()
    return result
