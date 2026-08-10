"""Template drafting: one draft per bucket, through the exact same graph a
single client's draft runs through.

Placeholder-filled facts never travel as state["facts"] (that payload is
scanned, and a placeholder token fails validation as a number). They travel
as synthetic chunks instead, rendered into the system prompt's "facts you
may cite" section, which is never scanned. has_cadence gets its own
prompt_builder since it drives a prohibition but can't live in facts either.

Nothing here records a touch or creates an outreach_message -- that happens
per client, at instantiation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

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
from app.campaigns.bucketing import Bucket, ProfileKey, derive_buckets
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.config import Settings
from app.db.models.message_template import MessageTemplate
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
) -> list[MessageTemplate]:
    """Derive this campaign's buckets and draft one template for each.

    Only accepted drafts are returned; a rejected bucket's run is still
    persisted (see draft_template) but has no template to instantiate from.
    """
    buckets = derive_buckets(session, campaign_id, limit=limit, context_loader=context_loader)
    templates = []
    for bucket in buckets:
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
    return templates
