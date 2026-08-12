"""Template drafting: one draft per bucket, through the exact same graph a
single client's draft runs through.

Placeholder-filled facts never travel as state["facts"] (that payload is
scanned, and a placeholder token fails validation as a number). They travel
as synthetic chunks instead, rendered into the system prompt's "facts you
may cite" section, which is never scanned. has_cadence gets its own
prompt_builder since it drives a prohibition but can't live in facts either.

Nothing here records a touch or creates an outreach_message -- that happens
per client, at instantiation.

draft_templates_for_campaign is limit-aware and top-up-safe: a bucket that
already has a non-rejected template for this campaign is skipped rather
than redrafted, and whatever is left is capped at the campaign's effective
limit, in a fixed order, so raising the limit and calling again fills in
the gap instead of duplicating work.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

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
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS
from app.audit.log import record_audit
from app.campaigns.bucketing import Bucket, ProfileKey, derive_buckets, profile_key_sort_key
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.template_policy import EffectivePolicy, effective_limit, get_effective_policy
from app.config import Settings
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
    """Draft one template for one bucket, and persist it if accepted.

    Returns None when every guardrail retry rejected the draft; the run is
    still persisted either way. The stamped client_id is one representative
    member of the bucket, not a claim the draft is about them alone.
    """
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

    if result.get("status") != "accepted":
        return None

    template = MessageTemplate(
        template_id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        profile_key=bucket.profile_key.as_dict(),
        ai_draft_content=run.ai_draft_content,
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
    return template


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


def _bucket_observed_volumes(
    session: Session, buckets: Sequence[Bucket]
) -> dict[ProfileKey, float]:
    """Total client_fund.observed_volume (the primary relationship's KES
    figure) across each bucket's members -- the tie-break when two buckets
    are the same size, so the one worth more of the book drafts first.
    """
    client_ids = {member.enrollment.client_id for bucket in buckets for member in bucket.members}
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
        bucket.profile_key: sum(
            volume_by_client.get(member.enrollment.client_id, 0.0) for member in bucket.members
        )
        for bucket in buckets
    }


def _ordered_by_priority(
    buckets: Sequence[Bucket], volumes: Mapping[ProfileKey, float]
) -> list[Bucket]:
    """Bucket size descending, then total observed volume descending, then
    profile key sorted -- deterministic and stable across runs, so "the
    first N of M" names the same buckets every time.
    """
    return sorted(
        buckets,
        key=lambda b: (
            -b.size,
            -volumes.get(b.profile_key, 0.0),
            profile_key_sort_key(b.profile_key),
        ),
    )


def draft_templates_for_campaign(
    session: Session,
    campaign_id: int,
    *,
    settings: Settings,
    llm_client: LLMClient,
    limit: int = DEFAULT_BATCH_LIMIT,
    context_loader: ContextLoader | None = None,
    guardrail_checks: Sequence[GuardrailCheck] = DEFAULT_GUARDRAIL_CHECKS,
    audit: AuditSink | None = None,
    tracer: Tracer | None = None,
) -> TemplateDraftOutcome:
    """Derive this campaign's buckets and draft one template for each due
    bucket, in deterministic order, up to the campaign's effective limit.

    A bucket that already has a non-rejected template for this campaign is
    skipped rather than redrafted, so raising the limit and calling this
    again tops up instead of duplicating. Only accepted drafts land in
    outcome.templates; a rejected bucket's run is still persisted (see
    draft_template) but has no template to instantiate from, and counts
    toward failed_guardrails instead. Persists one template_generation_plan
    row recording the three numbers -- estimated, limit, actual -- and the
    policy in force, so a later question about "why only 38" has an answer.
    """
    buckets = derive_buckets(session, campaign_id, limit=limit, context_loader=context_loader)
    estimated_templates = len(buckets)

    policy = get_effective_policy(session, campaign_id)
    draft_limit = effective_limit(
        estimated_templates,
        max_templates=policy.max_templates,
        max_templates_pct=policy.max_templates_pct,
    )

    existing = _existing_profile_key_fingerprints(session, campaign_id)
    candidates = [
        bucket
        for bucket in buckets
        if _profile_key_fingerprint(bucket.profile_key.as_dict()) not in existing
    ]
    skipped_existing = len(buckets) - len(candidates)

    volumes = _bucket_observed_volumes(session, candidates)
    ordered = _ordered_by_priority(candidates, volumes)
    to_draft = ordered if draft_limit is None else ordered[:draft_limit]

    templates: list[MessageTemplate] = []
    failed_guardrails = 0
    for bucket in to_draft:
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
        if template is not None:
            templates.append(template)
        else:
            failed_guardrails += 1

    plan = TemplateGenerationPlan(
        campaign_id=campaign_id,
        estimated_templates=estimated_templates,
        effective_limit=draft_limit,
        drafted_count=len(templates),
        skipped_existing=skipped_existing,
        failed_guardrails=failed_guardrails,
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
            "effective_limit": draft_limit,
            "drafted_count": len(templates),
            "skipped_existing": skipped_existing,
            "failed_guardrails": failed_guardrails,
        },
    )

    return TemplateDraftOutcome(
        templates=templates,
        estimated_templates=estimated_templates,
        effective_limit=draft_limit,
        drafted_count=len(templates),
        skipped_existing=skipped_existing,
        failed_guardrails=failed_guardrails,
        policy=policy,
    )
