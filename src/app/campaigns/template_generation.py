from __future__ import annotations

import functools
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import (
    AngleBrief,
    FormatContract,
    build_system_prompt,
    placeholder_token,
)
from app.agents.graph import (
    ClientContext,
    ContextLoader,
    GenerationState,
    GuardrailCheck,
    build_generation_graph,
    load_client_context,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS
from app.audit.log import record_audit
from app.campaigns.bucketing import (
    Bucket,
    BucketMember,
    ProfileKey,
    profile_key_sort_key,
)
from app.campaigns.estimation import DEFAULT_ESTIMATE_LIMIT, resolve_due_profile_keys
from app.campaigns.generation import resolve_product
from app.campaigns.template_policy import EffectivePolicy, effective_limit, get_effective_policy
from app.config import Settings
from app.db.models.campaigns import Enrollment
from app.db.models.message_template import MessageTemplate
from app.db.models.models import ClientFund
from app.db.models.template_generation_plan import TemplateGenerationPlan
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.tracing import NullTracer, Tracer
from app.llmops.versions import persist_generation_run
from app.privacy.boundary import AuditSink
from app.privacy.fact_block import FUND_DISPLAY_NAMES
from app.privacy.llm_client import LLMClient
from app.rag.grounding import GroundingChunk

logger = structlog.get_logger(__name__)

# label, placeholder field name -- the five facts every bucket may stand a
# token in for, regardless of whether this profile happens to use them.
_PLACEHOLDER_CHUNK_LABELS = (
    ("Typical contribution", "typical_contribution"),
    ("Largest contribution", "largest_contribution"),
    ("Years since exit", "years_since_exit"),
    ("Days held after the last top-up", "days_held_after_last_topup"),
    ("The month they left", "month_they_left"),
)


@dataclass(frozen=True)
class _PlaceholderChunk:
    """A synthetic chunk carrying a placeholder token instead of a retrieved figure."""

    chunk_id: int
    text: str


def bucket_placeholder_chunks(profile_key: ProfileKey) -> tuple[GroundingChunk, ...]:
    """One synthetic chunk per placeholder-filled fact. The cadence interval
    only appears when the bucket actually has one.
    """
    chunks = [
        _PlaceholderChunk(chunk_id=-(index + 1), text=f"{label}: {placeholder_token(field)}")
        for index, (label, field) in enumerate(_PLACEHOLDER_CHUNK_LABELS)
    ]
    if profile_key.has_cadence:
        chunks.append(
            _PlaceholderChunk(
                chunk_id=-(len(chunks) + 1),
                text=f"Cadence interval in days: {placeholder_token('cadence_interval_days')}",
            )
        )
    return tuple(chunks)


def bucket_facts(profile_key: ProfileKey) -> dict[str, Any]:
    """The real, shared facts a bucket draft's scanned payload may carry.
    Deliberately narrow: nothing placeholder-filled belongs here.
    """
    facts: dict[str, Any] = {"stale_contact": profile_key.stale_contact}
    if profile_key.exit_reason_charge_settled:
        facts["exit_reason"] = "charge_settled"
    if profile_key.fund_name_known:
        # product is fund_type with underscores turned to spaces; undo that.
        fund_name = FUND_DISPLAY_NAMES.get(profile_key.product.replace(" ", "_"))
        if fund_name is not None:
            facts["fund_name"] = fund_name
    return facts


def bucket_context(bucket: Bucket) -> ClientContext:
    """The ClientContext a bucket draft is generated against.

    angle, brief, contract, tier, and version fields are pure functions of
    (angle, tier, product), so the first member's context already carries
    them. Only facts and chunks change.
    """
    representative = bucket.members[0].context
    return replace(
        representative,
        raw_context={},
        facts=bucket_facts(bucket.profile_key),
        chunks=(*representative.chunks, *bucket_placeholder_chunks(bucket.profile_key)),
    )


def _bucket_prompt_builder(profile_key: ProfileKey) -> Callable[..., str]:
    """Same assembly as build_system_prompt, but the conditional prohibition
    is decided from the profile's own booleans, not the facts dict the
    graph passes in.
    """
    prohibition_facts = {
        "invested_every_n_days": 1 if profile_key.has_cadence else None,
        "stale_contact": profile_key.stale_contact,
        "exit_reason": "charge_settled" if profile_key.exit_reason_charge_settled else None,
    }

    def build(
        *,
        angle: str | None,
        prompt_variant: str | None,
        chunks: Sequence[GroundingChunk] = (),
        brief: AngleBrief | None = None,
        contract: FormatContract | None = None,
        facts: Any = None,
    ) -> str:
        return build_system_prompt(
            angle=angle,
            prompt_variant=prompt_variant,
            chunks=chunks,
            brief=brief,
            contract=contract,
            facts=prohibition_facts,
        )

    return build


def draft_template(
    session: Session,
    bucket: Bucket,
    *,
    campaign_id: int,
    settings: Settings,
    llm_client: LLMClient,
    guardrail_checks: Sequence[GuardrailCheck] = DEFAULT_GUARDRAIL_CHECKS,
    audit: AuditSink | None = None,
    tracer: Tracer | None = None,
) -> MessageTemplate | None:
    context = bucket_context(bucket)
    representative_client_id = bucket.members[0].enrollment.client_id
    tracer = tracer or NullTracer()

    def loader(client_id: int, product: str) -> ClientContext:
        return context

    graph = build_generation_graph(
        context_loader=loader,
        llm_client=llm_client,
        guardrail_checks=guardrail_checks,
        prompt_builder=_bucket_prompt_builder(bucket.profile_key),
        audit=audit,
        tracer=tracer,
    )
    state: GenerationState = new_generation_state(
        client_id=representative_client_id, product=bucket.profile_key.product
    )
    try:
        result = graph.invoke(state)
    finally:
        tracer.flush()

    run = persist_generation_run(session, result, settings)
    session.flush()
    persist_generation_telemetry(session, run, result, tracer=tracer)

    accepted = result.get("status") == "accepted"
    template = MessageTemplate(
        template_id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        profile_key=bucket.profile_key.as_dict(),
        ai_draft_content=run.ai_draft_content or {},
        status="pending_review" if accepted else "guardrail_rejected",
    )
    session.add(template)
    record_audit(
        session,
        entity_type="message_template",
        action="create",
        entity_id=template.template_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
    )
    session.flush()
    return template if accepted else None


@dataclass(frozen=True)
class TemplateDraftOutcome:
    """What one draft_templates_for_campaign call produced, and the plan
    behind it. Mirrors the template_generation_plan row this call persists.
    """

    templates: list[MessageTemplate]
    estimated_templates: int
    effective_limit: int | None
    drafted_count: int
    skipped_existing: int
    failed_guardrails: int
    failed_errors: int
    policy: EffectivePolicy


def _profile_key_fingerprint(data: Mapping[str, object]) -> str:
    """A canonical string for a profile_key dict, so two dicts that came
    from different places (a live ProfileKey vs. a stored JSONB row) compare
    equal regardless of key order.
    """
    return json.dumps(data, sort_keys=True, default=str)


def _existing_profile_key_fingerprints(session: Session, campaign_id: int) -> set[str]:
    """Every profile_key already carrying a non-rejected template for this
    campaign. A bucket matching one of these is a top-up, not a fresh draft.
    """
    rows = session.scalars(
        select(MessageTemplate.profile_key).where(
            MessageTemplate.campaign_id == campaign_id,
            MessageTemplate.status != "rejected",
        )
    )
    return {_profile_key_fingerprint(row) for row in rows}


@dataclass(frozen=True)
class _DueGroup:
    profile_key: ProfileKey
    client_ids: tuple[int, ...]
    representative: Enrollment

    @property
    def size(self) -> int:
        return len(self.client_ids)


def _group_due_clients(pairs: Sequence[tuple[Enrollment, ProfileKey]]) -> list[_DueGroup]:
    """Collect enrollment/profile pairs into one group per profile, keeping
    the first enrollment seen as the group's representative.
    """
    order: list[ProfileKey] = []
    members: dict[ProfileKey, list[Enrollment]] = {}
    for enrollment, key in pairs:
        if key not in members:
            order.append(key)
            members[key] = []
        members[key].append(enrollment)
    return [
        _DueGroup(
            profile_key=key,
            client_ids=tuple(e.client_id for e in members[key]),
            representative=members[key][0],
        )
        for key in order
    ]


def _group_observed_volumes(
    session: Session, groups: Sequence[_DueGroup]
) -> dict[ProfileKey, float]:
    """Total client_fund.observed_volume (the primary relationship's KES
    figure) across each group's clients -- the tie-break when two groups
    are the same size, so the one worth more of the book drafts first.
    """
    client_ids = {client_id for group in groups for client_id in group.client_ids}
    if not client_ids:
        return {}
    volume_by_client = dict(
        session.execute(
            select(ClientFund.client_id, ClientFund.observed_volume).where(
                ClientFund.client_id.in_(client_ids),
                ClientFund.is_primary_contact_row.is_(True),
            )
        ).all()
    )
    return {
        group.profile_key: sum(
            volume_by_client.get(client_id, 0.0) or 0.0 for client_id in group.client_ids
        )
        for group in groups
    }


def _ordered_by_priority(
    groups: Sequence[_DueGroup], volumes: Mapping[ProfileKey, float]
) -> list[_DueGroup]:
    return sorted(
        groups,
        key=lambda g: (
            -g.size,
            -volumes.get(g.profile_key, 0.0),
            profile_key_sort_key(g.profile_key),
        ),
    )


def _representative_bucket(
    session: Session, group: _DueGroup, context_loader: ContextLoader
) -> Bucket | None:
    enrollment = group.representative
    product = resolve_product(session, enrollment.client_id)
    try:
        context = context_loader(enrollment.client_id, product)
    except ValueError:
        return None
    key = group.profile_key
    if context.angle != key.message_angle or context.priority_tier != key.priority_tier:
        return None
    return Bucket(
        profile_key=group.profile_key,
        members=[BucketMember(enrollment=enrollment, context=context)],
    )


def draft_templates_for_campaign(
    session: Session,
    campaign_id: int,
    *,
    settings: Settings,
    llm_client: LLMClient,
    discovery_limit: int = DEFAULT_ESTIMATE_LIMIT,
    context_loader: ContextLoader | None = None,
    guardrail_checks: Sequence[GuardrailCheck] = DEFAULT_GUARDRAIL_CHECKS,
    audit: AuditSink | None = None,
    tracer: Tracer | None = None,
) -> TemplateDraftOutcome:
    context_loader = context_loader or functools.partial(load_client_context, session)
    groups = _group_due_clients(
        resolve_due_profile_keys(session, campaign_id, limit=discovery_limit)
    )
    estimated_templates = len(groups)

    policy = get_effective_policy(session, campaign_id)
    draft_limit = effective_limit(
        estimated_templates,
        max_templates=policy.max_templates,
        max_templates_pct=policy.max_templates_pct,
    )

    existing = _existing_profile_key_fingerprints(session, campaign_id)
    candidates = [
        group
        for group in groups
        if _profile_key_fingerprint(group.profile_key.as_dict()) not in existing
    ]
    skipped_existing = len(groups) - len(candidates)

    volumes = _group_observed_volumes(session, candidates)
    ordered = _ordered_by_priority(candidates, volumes)
    to_draft = ordered if draft_limit is None else ordered[:draft_limit]

    templates: list[MessageTemplate] = []
    failed_guardrails = 0
    failed_errors = 0
    for group in to_draft:
        # Committed per bucket rather than once at the end: an unexpected
        # error from one bucket (a provider timeout, a network blip) is
        # caught and counted rather than left to propagate, so it does not
        # roll back every template already drafted ahead of it in this
        # call. A caught bucket is simply not drafted this run; it is
        # picked up again the next time this endpoint is called.
        try:
            bucket = _representative_bucket(session, group, context_loader)
            if bucket is None:
                logger.warning(
                    "draft_templates_for_campaign.no_representative",
                    campaign_id=campaign_id,
                    profile_key=group.profile_key.as_dict(),
                )
                failed_errors += 1
                continue
            template = draft_template(
                session,
                bucket,
                campaign_id=campaign_id,
                settings=settings,
                llm_client=llm_client,
                guardrail_checks=guardrail_checks,
                audit=audit,
                tracer=tracer,
            )
        except Exception:
            session.rollback()
            logger.exception(
                "draft_templates_for_campaign.draft_failed",
                campaign_id=campaign_id,
                profile_key=group.profile_key.as_dict(),
            )
            failed_errors += 1
            continue

        if template is not None:
            templates.append(template)
        else:
            failed_guardrails += 1
        session.commit()

    plan = TemplateGenerationPlan(
        campaign_id=campaign_id,
        estimated_templates=estimated_templates,
        discovery_limit=discovery_limit,
        effective_limit=draft_limit,
        drafted_count=len(templates),
        skipped_existing=skipped_existing,
        failed_guardrails=failed_guardrails,
        failed_errors=failed_errors,
        policy_source=policy.source,
        policy_max_templates=policy.max_templates,
        policy_max_templates_pct=policy.max_templates_pct,
    )
    session.add(plan)
    session.flush()
    record_audit(
        session,
        entity_type="template_generation_plan",
        action="create",
        entity_id=str(plan.plan_id),
        detail={
            "campaign_id": campaign_id,
            "estimated_templates": estimated_templates,
            "discovery_limit": discovery_limit,
            "effective_limit": draft_limit,
            "drafted_count": len(templates),
            "skipped_existing": skipped_existing,
            "failed_guardrails": failed_guardrails,
            "failed_errors": failed_errors,
        },
    )

    return TemplateDraftOutcome(
        templates=templates,
        estimated_templates=estimated_templates,
        effective_limit=draft_limit,
        drafted_count=len(templates),
        skipped_existing=skipped_existing,
        failed_guardrails=failed_guardrails,
        failed_errors=failed_errors,
        policy=policy,
    )
