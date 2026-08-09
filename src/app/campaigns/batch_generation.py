"""Batch generation: draft many clients' emails through the model provider's
async batch endpoint in one submission, instead of one model call per client.

An alternative to campaigns.generation, not a replacement for it:
/campaigns/{id}/generate still drafts one client at a time, synchronously,
exactly as before. Reach for this module when a cohort is large enough that
holding one HTTP request open for one model call per client stops being
practical -- a provider batch runs the whole cohort as one submission, off
the request path, at a lower per-token cost.

Submission and ingestion are two separate calls, because the provider's
batch can take up to a day to finish:

- submit_batch selects this campaign's due, eligible enrollments, resolves
  each client's context exactly as the synchronous path does (angle, tier,
  facts, retrieved chunks, rendered system prompt), scans it inbound the
  same way run_model_boundary does, and sends the whole cohort as one
  request to the provider. Nothing is drafted yet.
- ingest_batch, called once the provider reports the batch has ended, reads
  each result, scans it outbound, runs the same guardrail checks the
  synchronous path's graph runs, and persists exactly the same
  generation_runs and outreach_messages rows -- so a batch-drafted message
  is reviewed exactly like any other, indistinguishable once it reaches the
  queue.

A rejected result is not retried automatically: the synchronous path's
guardrail retry is a second model call in the same request, which a batch
result has no equivalent of. A rejected item here is a terminal outcome,
the same as the synchronous path's message-less touch when every retry was
rejected -- see campaigns.touch's generate_touch docstring for that known
limitation, which applies here too.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import SystemPromptBlocks, build_system_prompt_blocks
from app.agents.graph import ClientContext, ContextLoader, load_client_context
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS, GuardrailFailure
from app.audit.log import record_audit
from app.campaigns.eligibility import check_eligibility
from app.campaigns.generation import model_boundary_audit_sink, resolve_product
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT, select_due_enrollments
from app.campaigns.touch import record_touch
from app.config import Settings
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.tracing import NullTracer, Tracer
from app.llmops.versions import persist_generation_run
from app.privacy.boundary import prepare_batch_payload, scan_batch_result, to_model_context
from app.privacy.llm_client import (
    build_batch_request,
    get_anthropic_batch_client,
    render_model_context,
)
from app.privacy.scanners import InboundLeak, OutboundLeak
from app.schemas.email_draft import DraftValidationError, parse_email_draft
from app.services.review import create_outreach_message

# Every module outside app.privacy is barred from importing the provider SDK
# directly (tests/test_privacy_boundary.py enforces it), so this module never
# imports anthropic.types itself -- build_batch_request shapes the request,
# and a batch result arrives as whatever object the provider's SDK returns,
# read here only through getattr/duck typing, never through an import.


# Terminal on the provider's side; "canceling" is not, so it is left out of
# both -- a batch mid-cancel is neither done nor safe to ingest yet.
_PROVIDER_ENDED_STATUSES = ("ended",)


class BatchNotFound(Exception):
    """No generation_batch exists with the given id."""


@dataclass(frozen=True)
class _SnapshotChunk:
    """A chunk reconstructed from a stored context_snapshot, for the guardrail
    checks that read chunk.chunk_id / chunk.text (rag.grounding.GroundingChunk).
    """

    chunk_id: int
    text: str


@dataclass(frozen=True)
class _SnapshotContract:
    """A tier contract reconstructed from a stored context_snapshot, for the
    format guardrail that reads contract.max_words.
    """

    max_words: int
    sign_off: str


@dataclass(frozen=True)
class BatchIngestOutcome:
    """What happened to one item during one ingest_batch call."""

    custom_id: str
    status: str  # "accepted" | "rejected"
    reason: str | None = None


@dataclass(frozen=True)
class BatchIngestResult:
    """What ingest_batch did: the batch's own row, plus one outcome per item
    actually processed this call. Empty outcomes with the batch still
    "submitted" or "in_progress" means the provider has not finished yet;
    call again later.
    """

    batch: GenerationBatch
    outcomes: list[BatchIngestOutcome] = field(default_factory=list)


def _context_snapshot(
    context: ClientContext, *, system_prompt_blocks: SystemPromptBlocks, model_payload: dict
) -> dict:
    """Everything ingest_batch needs to reconstruct this client's generation
    state, JSON-serialisable, captured now so a result read back a day later
    is still judged against the facts it was actually drafted from.

    system_prompt_cached/system_prompt_dynamic are kept separate rather than
    pre-joined, so a re-submitted retry (or a future caller) can still tell
    which half was the cache-eligible one; "system_prompt" is the two
    joined the same way build_batch_request sends them, for telemetry and
    tracing, which only care what the model actually saw as one prompt.
    """
    system_prompt = (
        f"{system_prompt_blocks.cached}\n\n{system_prompt_blocks.dynamic}"
        if system_prompt_blocks.dynamic
        else system_prompt_blocks.cached
    )
    return {
        "angle": context.angle,
        "prompt_variant": context.prompt_variant,
        "priority_tier": context.priority_tier,
        "rule_version": context.rule_version,
        "angle_catalog_version": context.angle_catalog_version,
        "data_date": context.data_date.isoformat() if context.data_date else None,
        "facts": context.facts,
        "chunks": [{"chunk_id": c.chunk_id, "text": c.text} for c in context.chunks],
        "contract": (
            {"max_words": context.contract.max_words, "sign_off": context.contract.sign_off}
            if context.contract is not None
            else None
        ),
        "system_prompt": system_prompt,
        "system_prompt_cached": system_prompt_blocks.cached,
        "system_prompt_dynamic": system_prompt_blocks.dynamic,
        "model_payload": model_payload,
        "tool_calls": [
            {
                "tool_name": "context_fetch",
                "input": {"angle": context.angle},
                "output": {"angle": context.angle, "prompt_variant": context.prompt_variant},
            },
            {
                "tool_name": "rag_retrieval",
                "input": {"angle": context.angle},
                "output": {
                    "chunk_count": len(context.chunks),
                    "chunks": [{"chunk_id": c.chunk_id, "text": c.text} for c in context.chunks],
                },
            },
        ],
    }


def _already_batched(session: Session, enrollment_id: int, step_no: int) -> bool:
    """True when some earlier, still-unresolved batch already claimed this
    step, so submit_batch does not bundle the same client into two batches
    at once and risk two outreach_messages for one touch.
    """
    return (
        session.execute(
            select(GenerationBatchItem.generation_batch_item_id)
            .join(
                GenerationBatch,
                GenerationBatch.generation_batch_id == GenerationBatchItem.generation_batch_id,
            )
            .where(
                GenerationBatchItem.enrollment_id == enrollment_id,
                GenerationBatchItem.step_no == step_no,
                GenerationBatch.status.not_in(("failed",)),
            )
            .limit(1)
        ).first()
        is not None
    )


def _build_request(
    session: Session,
    batch: GenerationBatch,
    enrollment: Enrollment,
    step_no: int,
    *,
    settings: Settings,
    context_loader: ContextLoader,
    tracer: Tracer,
    audit,
) -> Any | None:
    """One client's provider request, or None when this client is skipped
    (an inbound leak, or no facts yet). A skip never raises: one bad client
    should not abort the whole batch. Adds the matching GenerationBatchItem
    row to session as a side effect whenever a request is returned.

    Traced the same way the synchronous graph traces retrieve_context and
    assemble_prompt: both spans land under this item's trace_id now, at
    submit time, since that is when the work they cover actually happens.
    The matching generate/guardrails spans are added later, by
    ingest_batch, once there is a result to trace them against.
    """
    run_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    product = resolve_product(session, enrollment.client_id)

    retrieve_span = tracer.start_span(
        trace_id=trace_id,
        name="retrieve_context",
        input={"client_id": enrollment.client_id, "product": product},
        metadata={"run_id": run_id, "client_id": enrollment.client_id, "product": product},
    )
    try:
        context = context_loader(enrollment.client_id, product)
    except ValueError:
        tracer.end_span(retrieve_span, output={"error": "no llm_client_context row"})
        return None
    tracer.end_span(
        retrieve_span,
        output={
            "angle": context.angle,
            "prompt_variant": context.prompt_variant,
            "chunk_count": len(context.chunks),
        },
    )

    assemble_span = tracer.start_span(
        trace_id=trace_id,
        name="assemble_prompt",
        input={"angle": context.angle, "prompt_variant": context.prompt_variant},
    )
    prompt_blocks = build_system_prompt_blocks(
        angle=context.angle,
        prompt_variant=context.prompt_variant,
        chunks=context.chunks,
        brief=context.brief,
        contract=context.contract,
        facts=context.facts,
    )
    raw_payload = dict(context.facts) if context.facts else to_model_context(context.raw_context)
    tracer.end_span(
        assemble_span,
        output={
            "system_prompt_cached": prompt_blocks.cached,
            "system_prompt_dynamic": prompt_blocks.dynamic,
        },
    )

    try:
        payload = prepare_batch_payload(
            raw_payload,
            entity_id=str(enrollment.client_id),
            run_id=run_id,
            trace_id=trace_id,
            audit=audit,
        )
    except InboundLeak:
        return None

    item = GenerationBatchItem(
        generation_batch_id=batch.generation_batch_id,
        custom_id=run_id,
        client_id=enrollment.client_id,
        enrollment_id=enrollment.enrollment_id,
        step_no=step_no,
        trace_id=trace_id,
        context_snapshot=_context_snapshot(
            context, system_prompt_blocks=prompt_blocks, model_payload=payload
        )
        | {"product": product},
    )
    session.add(item)
    return build_batch_request(
        custom_id=run_id,
        system_cached=prompt_blocks.cached,
        system_dynamic=prompt_blocks.dynamic,
        user=render_model_context(payload),
        settings=settings,
    )


def submit_batch(
    session: Session,
    campaign_id: int,
    *,
    settings: Settings,
    limit: int = DEFAULT_BATCH_LIMIT,
    context_loader: ContextLoader | None = None,
    tracer: Tracer | None = None,
    client: Any | None = None,
) -> GenerationBatch:
    """Select this campaign's due, eligible enrollments and submit one draft
    request per client to the provider's batch endpoint in a single call.

    limit caps how many enrollments this one submission can include, the
    same knob /campaigns/{id}/generate already exposes; whatever is left
    stays due for the next submit_batch call. A touch is logged for every
    enrollment actually bundled, so a second submission before this one is
    ingested does not pick the same client up twice.

    context_loader defaults to the real load_client_context bound to this
    session, the same ContextLoader seam agents.email_channel.EmailAgent
    takes; tracer defaults to a no-op, the same as EmailAgent; client
    defaults to the real provider client. A test passes fakes for all
    three, exactly as it would pass a fake context_loader and
    ScriptedLLMClient into EmailAgent for the synchronous path.

    Assumes campaign_id already names a real campaign; the caller (see
    services.campaigns.submit_campaign_batch) checks that first, the same
    place run_campaign_generation's check lives.
    """
    context_loader = context_loader or functools.partial(load_client_context, session)
    tracer = tracer or NullTracer()
    batch = GenerationBatch(
        generation_batch_id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        provider=settings.llm_provider,
        status="building",
        requested_limit=limit,
    )
    session.add(batch)
    session.flush()

    audit = model_boundary_audit_sink(session)
    due = select_due_enrollments(session, campaign_id=campaign_id, limit=limit)
    requests: list[Any] = []
    for enrollment in due:
        result = check_eligibility(session, enrollment)
        if not result.eligible:
            continue

        step_no = enrollment.current_step + 1
        if _already_batched(session, enrollment.enrollment_id, step_no):
            continue
        touch = record_touch(session, enrollment, step_no)
        if touch.message_id is not None:
            continue

        request = _build_request(
            session,
            batch,
            enrollment,
            step_no,
            settings=settings,
            context_loader=context_loader,
            tracer=tracer,
            audit=audit,
        )
        if request is not None:
            requests.append(request)

    tracer.flush()

    if not requests:
        batch.status = "no_eligible_clients"
        session.flush()
        record_audit(
            session,
            entity_type="generation_batch",
            action="submit",
            entity_id=batch.generation_batch_id,
            detail={"requested_count": 0, "reason": "no_eligible_clients"},
        )
        return batch

    client = client or get_anthropic_batch_client(settings)
    provider_batch = client.messages.batches.create(requests=requests)

    batch.provider_batch_id = provider_batch.id
    batch.status = "submitted"
    batch.requested_count = len(requests)
    batch.submitted_at = datetime.now(UTC)
    session.flush()
    record_audit(
        session,
        entity_type="generation_batch",
        action="submit",
        entity_id=batch.generation_batch_id,
        detail={
            "provider_batch_id": provider_batch.id,
            "requested_count": len(requests),
            "requested_limit": limit,
        },
    )
    return batch


def _guardrail_check_state(item: GenerationBatchItem, subject: str, body: str) -> dict[str, Any]:
    snapshot = item.context_snapshot
    contract = snapshot.get("contract")
    return {
        "subject": subject,
        "body": body,
        "facts": snapshot.get("facts"),
        "chunks": [_SnapshotChunk(**c) for c in snapshot.get("chunks", [])],
        "contract": _SnapshotContract(**contract) if contract else None,
    }


def _persist_result(
    session: Session,
    batch: GenerationBatch,
    item: GenerationBatchItem,
    *,
    settings: Settings,
    tracer: Tracer,
    status: str,
    reason: str | None,
    failed_guardrail: str | None = None,
    raw_output: str | None = None,
    structured: dict | None = None,
    usage: tuple[int | None, int | None] = (None, None),
) -> BatchIngestOutcome:
    snapshot = item.context_snapshot
    data_date_raw = snapshot.get("data_date")
    llm_calls = [
        {
            "attempt": 1,
            "system_prompt": snapshot["system_prompt"],
            "raw_output": raw_output,
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            # A batch call has no meaningful single-request latency to
            # report; 0 marks a batch-sourced call rather than leaving a
            # non-null column implying a real measurement.
            "latency_ms": 0,
        }
    ]
    state = {
        "run_id": item.custom_id,
        "trace_id": item.trace_id,
        "client_id": item.client_id,
        "product": snapshot.get("product"),
        "priority_tier": snapshot.get("priority_tier"),
        "data_date": date.fromisoformat(data_date_raw) if data_date_raw else None,
        "rule_version": snapshot.get("rule_version"),
        "angle_catalog_version": snapshot.get("angle_catalog_version"),
        "prompt_variant": snapshot.get("prompt_variant"),
        "angle": snapshot.get("angle"),
        "status": status,
        "attempts": 1,
        "failed_guardrail": failed_guardrail,
        "reason": reason,
        "raw_structured_output": structured,
        "llm_calls": llm_calls,
        "tool_calls": snapshot.get("tool_calls", []),
    }

    run = persist_generation_run(session, state, settings)
    session.flush()
    persist_generation_telemetry(session, run, state, tracer=tracer)

    if status == "accepted":
        message = create_outreach_message(session, run, campaign_id=batch.campaign_id)
        touch = session.execute(
            select(TouchLog).where(
                TouchLog.enrollment_id == item.enrollment_id, TouchLog.step_no == item.step_no
            )
        ).scalar_one_or_none()
        if touch is not None:
            touch.message_id = message.message_id

    item.status = status
    item.processed_at = datetime.now(UTC)
    session.flush()
    return BatchIngestOutcome(custom_id=item.custom_id, status=status, reason=reason)


def _ingest_one(
    session: Session,
    batch: GenerationBatch,
    item: GenerationBatchItem,
    result,
    *,
    settings: Settings,
    tracer: Tracer,
) -> BatchIngestOutcome:
    """Turn one provider result into a generation run: scan it outbound, run
    the same guardrails the synchronous path runs, and persist accordingly.

    Traced the same way the synchronous graph traces generate and
    guardrails, under the trace_id retrieve_context/assemble_prompt were
    already logged against at submit time -- so a batch-drafted run shows
    up in Langfuse as one trace, retrieve to guardrails, exactly like a
    synchronous one, even though the two halves were recorded a day apart.
    """
    outcome_type = result.result.type
    if outcome_type != "succeeded":
        return _persist_result(
            session,
            batch,
            item,
            settings=settings,
            tracer=tracer,
            status="rejected",
            reason=f"batch result {outcome_type}",
        )

    message = result.result.message
    raw_output = "".join(block.text for block in message.content if block.type == "text")
    usage = (
        (message.usage.input_tokens, message.usage.output_tokens)
        if getattr(message, "usage", None)
        else (None, None)
    )

    generate_span = tracer.start_span(
        trace_id=item.trace_id,
        name="generate",
        input={"system_prompt": item.context_snapshot.get("system_prompt")},
        as_type="generation",
        model=settings.llm_model,
    )
    tracer.end_span(
        generate_span,
        output=raw_output,
        usage_details=({"input": usage[0], "output": usage[1]} if usage[0] is not None else None),
    )

    if message.stop_reason == "refusal":
        return _persist_result(
            session,
            batch,
            item,
            settings=settings,
            tracer=tracer,
            status="rejected",
            reason="model declined the request",
            raw_output=raw_output,
            usage=usage,
        )

    audit = model_boundary_audit_sink(session)
    try:
        scan_batch_result(
            raw_output,
            entity_id=str(item.client_id),
            run_id=item.custom_id,
            trace_id=item.trace_id,
            audit=audit,
        )
    except OutboundLeak as leak:
        return _persist_result(
            session,
            batch,
            item,
            settings=settings,
            tracer=tracer,
            status="rejected",
            reason=str(leak),
            failed_guardrail="pii_scan",
            raw_output=raw_output,
            usage=usage,
        )

    facts = item.context_snapshot.get("facts")
    try:
        structured = parse_email_draft(raw_output, facts)
    except DraftValidationError as failure:
        return _persist_result(
            session,
            batch,
            item,
            settings=settings,
            tracer=tracer,
            status="rejected",
            reason=str(failure),
            failed_guardrail="structured_output",
            raw_output=raw_output,
            usage=usage,
        )

    check_state = _guardrail_check_state(item, structured.subject, structured.body)
    guardrails_span = tracer.start_span(
        trace_id=item.trace_id,
        name="guardrails",
        input={"subject": structured.subject, "body": structured.body},
    )
    for check in DEFAULT_GUARDRAIL_CHECKS:
        try:
            check(check_state)
        except GuardrailFailure as failure:
            tracer.end_span(
                guardrails_span,
                output={"status": "rejected", "failed_guardrail": failure.guardrail},
            )
            return _persist_result(
                session,
                batch,
                item,
                settings=settings,
                tracer=tracer,
                status="rejected",
                reason=str(failure),
                failed_guardrail=failure.guardrail,
                raw_output=raw_output,
                usage=usage,
            )
    tracer.end_span(guardrails_span, output={"status": "accepted"})

    return _persist_result(
        session,
        batch,
        item,
        settings=settings,
        tracer=tracer,
        status="accepted",
        reason=None,
        raw_output=raw_output,
        structured=structured.model_dump(),
        usage=usage,
    )


def ingest_batch(
    session: Session,
    generation_batch_id: str,
    *,
    settings: Settings,
    tracer: Tracer | None = None,
    client: Any | None = None,
) -> BatchIngestResult:
    """Check a submitted batch's status, and turn its results into
    generation_runs and outreach_messages once the provider reports it
    ended. Safe to call repeatedly: a batch not yet ended returns with no
    outcomes and an updated status; an already-ingested batch returns with
    no outcomes and does no work twice.

    tracer defaults to a no-op, the same as EmailAgent; client defaults to
    the real provider client. A test passes fakes for either.
    """
    tracer = tracer or NullTracer()
    batch = session.get(GenerationBatch, generation_batch_id)
    if batch is None:
        raise BatchNotFound(generation_batch_id)
    if batch.status in ("ingested", "no_eligible_clients", "failed"):
        return BatchIngestResult(batch=batch, outcomes=[])
    if batch.provider_batch_id is None:
        return BatchIngestResult(batch=batch, outcomes=[])

    client = client or get_anthropic_batch_client(settings)
    provider_batch = client.messages.batches.retrieve(batch.provider_batch_id)

    if provider_batch.processing_status not in _PROVIDER_ENDED_STATUSES:
        batch.status = "in_progress"
        session.flush()
        return BatchIngestResult(batch=batch, outcomes=[])

    items_by_custom_id = {
        item.custom_id: item
        for item in session.scalars(
            select(GenerationBatchItem).where(
                GenerationBatchItem.generation_batch_id == generation_batch_id,
                GenerationBatchItem.status == "pending",
            )
        )
    }

    outcomes: list[BatchIngestOutcome] = []
    for result in client.messages.batches.results(batch.provider_batch_id):
        item = items_by_custom_id.get(result.custom_id)
        if item is None:
            continue  # already ingested in an earlier, interrupted call
        outcomes.append(_ingest_one(session, batch, item, result, settings=settings, tracer=tracer))
    tracer.flush()

    counts = provider_batch.request_counts
    batch.succeeded_count = counts.succeeded
    batch.errored_count = counts.errored + counts.canceled + counts.expired
    batch.ended_at = datetime.now(UTC)
    batch.ingested_at = datetime.now(UTC)
    batch.status = "ingested"
    session.flush()
    record_audit(
        session,
        entity_type="generation_batch",
        action="ingest",
        entity_id=batch.generation_batch_id,
        detail={
            "succeeded_count": batch.succeeded_count,
            "errored_count": batch.errored_count,
            "accepted": sum(1 for o in outcomes if o.status == "accepted"),
            "rejected": sum(1 for o in outcomes if o.status == "rejected"),
        },
    )
    return BatchIngestResult(batch=batch, outcomes=outcomes)
