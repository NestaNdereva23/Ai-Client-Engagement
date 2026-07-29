"""The draft generation graph: retrieve context, assemble prompt, generate, guardrails"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import build_system_prompt
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS, GuardrailFailure
from app.db.models.rules import ClientMessageIndicators
from app.db.models.views import llm_client_context
from app.llmops.tracing import NullTracer, Tracer
from app.privacy.boundary import AuditSink, run_model_boundary, to_model_context
from app.privacy.llm_client import LLMClient, as_model_call
from app.privacy.scanners import OutboundLeak
from app.rag.grounding import GroundingChunk
from app.rag.retrieve import retrieve_product_facts
from app.schemas.email_draft import DraftValidationError, parse_email_draft

# The default prompt builder is EmailAgent's.
PromptBuilder = Callable[..., str]

# Guard against an endlessly retrying loop.
DEFAULT_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class ClientContext:
    """Everything retrieve_context needs for one client: masked tiers, angle, facts."""

    raw_context: Mapping[str, Any]
    angle: str
    prompt_variant: str
    chunks: Sequence[GroundingChunk]


# Loads a client's context; the caller binds a live session (e.g. via
# functools.partial(load_client_context, session))
ContextLoader = Callable[[int, str], ClientContext]


class GenerationState(TypedDict, total=False):
    """State threaded through the graph for one client's draft."""

    client_id: int
    product: str
    run_id: str
    trace_id: str
    angle: str | None
    prompt_variant: str | None
    raw_context: Mapping[str, Any]
    chunks: Sequence[GroundingChunk]
    context: dict[str, Any]
    system_prompt: str
    draft: str | None  # the model's raw output, unparsed, as generated
    subject: str | None
    body: str | None
    raw_structured_output: dict[str, Any] | None  # EmailDraft.model_dump(), for audit
    attempts: int
    status: str  # "pending" | "accepted" | "rejected"
    reason: str | None
    failed_guardrail: str | None  # "pii_scan" | "structured_output" | "grounding" | ...


# A guardrail check inspects the state and raises GuardrailFailure on a bad
# draft; it returns nothing on a pass.
GuardrailCheck = Callable[[GenerationState], None]


def new_generation_state(*, client_id: int, product: str) -> GenerationState:
    """Seed state for one run: fresh run_id/trace_id, zero attempts.
    trace_id is a bare 32-char hex uuid because
    that is also a valid Langfuse trace id
    """
    return {
        "client_id": client_id,
        "product": product,
        "run_id": str(uuid.uuid4()),
        "trace_id": uuid.uuid4().hex,
        "attempts": 0,
        "status": "pending",
        "reason": None,
        "failed_guardrail": None,
    }


def load_client_context(session: Session, client_id: int, product: str) -> ClientContext:
    """The default ContextLoader: read the masked view, resolved indicators, and RAG facts.
    Bind a session to get a ContextLoader
    """
    row = (
        session.execute(
            select(llm_client_context).where(llm_client_context.c.client_id == client_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError(f"no llm_client_context row for client {client_id!r}")

    indicators = session.get(ClientMessageIndicators, client_id)
    if indicators is None:
        raise ValueError(f"no resolved message indicators for client {client_id!r}")

    chunks = retrieve_product_facts(session, product=product, angle=indicators.message_angle)
    return ClientContext(
        raw_context=dict(row),
        angle=indicators.message_angle,
        prompt_variant=indicators.prompt_variant,
        chunks=chunks,
    )


def _traced(name: str, fn: Callable[[GenerationState], dict[str, Any]], tracer: Tracer):
    """Wrap a node so one Langfuse span covers its call, under the run's trace_id."""

    def wrapped(state: GenerationState) -> dict[str, Any]:
        metadata = (
            {
                "run_id": state["run_id"],
                "client_id": state["client_id"],
                "product": state["product"],
            }
            if name == "retrieve_context"
            else None
        )
        handle = tracer.start_span(
            trace_id=state["trace_id"], name=name, input=dict(state), metadata=metadata
        )
        result = fn(state)
        tracer.end_span(handle, output=result)
        return result

    return wrapped


def build_generation_graph(
    *,
    context_loader: ContextLoader,
    llm_client: LLMClient,
    guardrail_checks: Sequence[GuardrailCheck] = DEFAULT_GUARDRAIL_CHECKS,
    prompt_builder: PromptBuilder = build_system_prompt,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    audit: AuditSink | None = None,
    tracer: Tracer | None = None,
) -> CompiledStateGraph:
    """Wire the four nodes into a compiled graph, ready to invoke() per client.
    tracer defaults to a no-op (NullTracer), so building and running a graph
    never requires a live Langfuse instance
    """
    checks = tuple(guardrail_checks)
    tracer = tracer or NullTracer()

    def _retry_or_reject(attempts: int, guardrail: str, reason: str) -> dict[str, Any]:
        status = "pending" if attempts < max_attempts else "rejected"
        return {"status": status, "reason": reason, "failed_guardrail": guardrail}

    def retrieve_context(state: GenerationState) -> dict[str, Any]:
        client_context = context_loader(state["client_id"], state["product"])
        return {
            "raw_context": client_context.raw_context,
            "angle": client_context.angle,
            "prompt_variant": client_context.prompt_variant,
            "chunks": client_context.chunks,
        }

    def assemble_prompt(state: GenerationState) -> dict[str, Any]:
        context = to_model_context(state["raw_context"])
        prompt = prompt_builder(
            angle=state.get("angle"),
            prompt_variant=state.get("prompt_variant"),
            chunks=state.get("chunks", ()),
        )
        return {"context": context, "system_prompt": prompt}

    def generate(state: GenerationState) -> dict[str, Any]:
        attempts = state.get("attempts", 0) + 1
        model_call = as_model_call(llm_client, system=state["system_prompt"])
        try:
            draft = run_model_boundary(
                state["context"],
                model_call,
                entity_id=str(state["client_id"]),
                run_id=state["run_id"],
                trace_id=state["trace_id"],
                audit=audit,
            )
        except OutboundLeak as leak:
            return {"attempts": attempts, **_retry_or_reject(attempts, "pii_scan", str(leak))}
        return {
            "draft": draft,
            "attempts": attempts,
            "status": "pending",
            "reason": None,
            "failed_guardrail": None,
        }

    def route_after_generate(state: GenerationState) -> str:
        if state.get("status") == "rejected":
            return "done"
        if state.get("failed_guardrail") is not None:
            return "retry"
        return "guardrails"

    def guardrails(state: GenerationState) -> dict[str, Any]:
        attempts = state.get("attempts", 0)

        # Structured output validation comes first: a draft that is not valid
        # JSON, or is missing a field or a required placeholder, never even
        # reaches the pluggable checks below.
        try:
            structured = parse_email_draft(state.get("draft") or "")
        except DraftValidationError as failure:
            return _retry_or_reject(attempts, "structured_output", str(failure))

        updates: dict[str, Any] = {
            "subject": structured.subject,
            "body": structured.body,
            "raw_structured_output": structured.model_dump(),
        }
        check_state: GenerationState = {**state, **updates}

        for check in checks:
            try:
                check(check_state)
            except GuardrailFailure as failure:
                outcome = _retry_or_reject(attempts, failure.guardrail, str(failure))
                return {**updates, **outcome}

        return {**updates, "status": "accepted", "reason": None, "failed_guardrail": None}

    def route_after_guardrails(state: GenerationState) -> str:
        if state["status"] == "pending":
            return "retry"
        return "done"

    graph = StateGraph(GenerationState)
    graph.add_node("retrieve_context", _traced("retrieve_context", retrieve_context, tracer))
    graph.add_node("assemble_prompt", _traced("assemble_prompt", assemble_prompt, tracer))
    graph.add_node("generate", _traced("generate", generate, tracer))
    graph.add_node("guardrails", _traced("guardrails", guardrails, tracer))

    graph.add_edge(START, "retrieve_context")
    graph.add_edge("retrieve_context", "assemble_prompt")
    graph.add_edge("assemble_prompt", "generate")
    graph.add_conditional_edges(
        "generate",
        route_after_generate,
        {"guardrails": "guardrails", "retry": "generate", "done": END},
    )
    graph.add_conditional_edges(
        "guardrails",
        route_after_guardrails,
        {"retry": "generate", "done": END},
    )

    return graph.compile()
