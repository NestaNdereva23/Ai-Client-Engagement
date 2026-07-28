"""The draft generation graph: retrieve context, assemble prompt, generate, guardrails.

Four nodes run in a line for one client's draft. The model is only ever
reached through run_model_boundary (app.privacy), so a leak aborts the whole
run rather than being treated as a retryable guardrail failure. A guardrail
failure is different: it means the draft itself is bad (ungrounded, wrong
shape), so it loops back to generate up to a retry limit, then the run is
rejected.

State carries run_id and trace_id, set once at entry and unchanged through
every node, so LLMOpsand the audit log (already indexed on both
columns) can join a generation run to its boundary crossings.
"""

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
from app.db.models.rules import ClientMessageIndicators
from app.db.models.views import llm_client_context
from app.privacy.boundary import AuditSink, run_model_boundary, to_model_context
from app.privacy.llm_client import LLMClient, as_model_call
from app.rag.grounding import GroundingChunk, UngroundedClaim, enforce_grounding
from app.rag.retrieve import retrieve_product_facts
from app.schemas.email_draft import DraftValidationError, parse_email_draft

# The default prompt builder is EmailAgent
PromptBuilder = Callable[..., str]

# Guard against an endlessly retrying loop
DEFAULT_MAX_ATTEMPTS = 2


class GuardrailFailure(Exception):
    """Raised by a guardrail check when a draft fails it."""


@dataclass(frozen=True)
class ClientContext:
    """Everything retrieve_context needs for one client: masked tiers, angle, facts."""

    raw_context: Mapping[str, Any]
    angle: str
    prompt_variant: str
    chunks: Sequence[GroundingChunk]


# Loads a client's context; the caller binds a live session (e.g. via
# functools.partial(load_client_context, session)) so this module never has
# to know about SQLAlchemy directly.
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


# A guardrail check inspects the state and raises GuardrailFailure on a bad
# draft it returns nothing on a pass.
GuardrailCheck = Callable[[GenerationState], None]


def new_generation_state(*, client_id: int, product: str) -> GenerationState:
    """Seed state for one run: fresh run_id/trace_id, zero attempts."""
    return {
        "client_id": client_id,
        "product": product,
        "run_id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "attempts": 0,
        "status": "pending",
        "reason": None,
    }


def default_grounding_check(state: GenerationState) -> None:
    """Every rate or return claim in the email body must trace to a retrieved chunk."""
    try:
        enforce_grounding(state.get("body") or "", state.get("chunks", []))
    except UngroundedClaim as exc:
        raise GuardrailFailure(str(exc)) from exc


def load_client_context(session: Session, client_id: int, product: str) -> ClientContext:
    """The default ContextLoader: read the masked view, resolved indicators, and RAG facts.

    Bind a session to get a ContextLoader, e.g.
    functools.partial(load_client_context, session).
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


def build_generation_graph(
    *,
    context_loader: ContextLoader,
    llm_client: LLMClient,
    guardrail_checks: Sequence[GuardrailCheck] = (default_grounding_check,),
    prompt_builder: PromptBuilder = build_system_prompt,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    audit: AuditSink | None = None,
) -> CompiledStateGraph:
    """Wire the four nodes into a compiled graph, ready to invoke() per client."""
    checks = tuple(guardrail_checks)

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
        model_call = as_model_call(llm_client, system=state["system_prompt"])
        draft = run_model_boundary(
            state["context"],
            model_call,
            entity_id=str(state["client_id"]),
            run_id=state["run_id"],
            trace_id=state["trace_id"],
            audit=audit,
        )
        return {"draft": draft, "attempts": state.get("attempts", 0) + 1}

    def _retry_or_reject(state: GenerationState, reason: str) -> dict[str, Any]:
        if state.get("attempts", 0) < max_attempts:
            return {"status": "pending", "reason": reason}
        return {"status": "rejected", "reason": reason}

    def guardrails(state: GenerationState) -> dict[str, Any]:
        # Structured-output validation comes first: a draft that is not valid
        # JSON, or is missing a field or a required placeholder, never even
        # reaches the pluggable checks below.
        try:
            structured = parse_email_draft(state.get("draft") or "")
        except DraftValidationError as failure:
            return _retry_or_reject(state, str(failure))

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
                return {**updates, **_retry_or_reject(state, str(failure))}

        return {**updates, "status": "accepted", "reason": None}

    def route_after_guardrails(state: GenerationState) -> str:
        if state["status"] == "pending":
            return "retry"
        return "done"

    graph = StateGraph(GenerationState)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("assemble_prompt", assemble_prompt)
    graph.add_node("generate", generate)
    graph.add_node("guardrails", guardrails)

    graph.add_edge(START, "retrieve_context")
    graph.add_edge("retrieve_context", "assemble_prompt")
    graph.add_edge("assemble_prompt", "generate")
    graph.add_edge("generate", "guardrails")
    graph.add_conditional_edges(
        "guardrails",
        route_after_guardrails,
        {"retry": "generate", "done": END},
    )

    return graph.compile()
