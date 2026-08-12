"""The draft generation graph: retrieve context, assemble prompt, generate, guardrails"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import build_system_prompt
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS, GuardrailFailure
from app.db.models.rules import ClientMessageIndicators
from app.db.models.views import llm_client_context, llm_client_numeric_facts
from app.llmops.tracing import NullTracer, Tracer
from app.privacy.boundary import AuditSink, run_model_boundary, to_model_context
from app.privacy.fact_block import FUND_DISPLAY_NAMES, ModelFactBlock
from app.privacy.llm_client import LLMClient, as_model_call
from app.privacy.scanners import OutboundLeak
from app.rag.grounding import GroundingChunk
from app.rag.retrieve import retrieve_product_facts
from app.rules.catalog import load_angle
from app.rules.tier_contract import load_tier
from app.schemas.email_draft import DraftValidationError, parse_email_draft
from app.transform.flatten import latest_reference_date

# The default prompt builder is EmailAgent's.
PromptBuilder = Callable[..., str]

# Guard against an endlessly retrying loop.
DEFAULT_MAX_ATTEMPTS = 2

# The two allow-listed views, split by which one carries each fact.
_BAND_FACT_KEYS = (
    "recency_band",
    "value_band",
    "cadence_band",
    "hold_band",
    "purchase_depth",
    "trend_band",
    "exit_reason",
    "fund_type",
    "in_wave",
    "has_depth",
    "staged_exit",
    "stale_contact",
    "newly_dormant",
)
_NUMERIC_FACT_KEYS = (
    "years_since_exit",
    "typical_contribution_kes",
    "largest_contribution_kes",
    "invested_every_n_days",
    "days_held_after_last_topup",
    "month_they_left",
)


@dataclass(frozen=True)
class ClientContext:
    """Everything retrieve_context needs for one client: masked tiers, angle, facts.

    brief, contract, facts, priority_tier, rule_version, angle_catalog_version
    and data_date all stay optional so a loader that predates the catalogue
    (or a test fake) still satisfies this shape. The last three feed the
    reproducibility stamp: which rule, which catalogue, and which data pull
    produced this client's angle.
    """

    raw_context: Mapping[str, Any]
    angle: str
    prompt_variant: str
    chunks: Sequence[GroundingChunk]
    brief: Any | None = None
    contract: Any | None = None
    facts: Mapping[str, Any] | None = None
    priority_tier: str | None = None
    rule_version: int | None = None
    angle_catalog_version: int | None = None
    data_date: date | None = None


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
    priority_tier: str | None
    prompt_variant: str | None
    rule_version: int | None
    angle_catalog_version: int | None
    data_date: date | None
    raw_context: Mapping[str, Any]
    chunks: Sequence[GroundingChunk]
    brief: Any | None
    contract: Any | None
    facts: Mapping[str, Any] | None
    context: dict[str, Any]
    system_prompt: str
    draft: str | None  # the model's raw output, unparsed, as generated
    subject: str | None
    body: str | None
    raw_structured_output: dict[str, Any] | None  # EmailDraft.model_dump(), for audit
    call_brief: str | None  # set for a tier whose contract adds one
    attempts: int
    status: str  # "pending" | "accepted" | "rejected"
    reason: str | None
    failed_guardrail: str | None  # "pii_scan" | "structured_output" | "grounding" | ...
    llm_calls: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]


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


def load_client_facts(
    session: Session, client_id: int, bands_row: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Assemble one client's fact block from the two allow-listed views.

    Routed through ModelFactBlock rather than handed over raw, so the amounts
    round and a cadence fact with no real cadence drops out before anything
    downstream can quote it. None when the client has no numeric row yet.
    """
    numeric = (
        session.execute(
            select(llm_client_numeric_facts).where(
                llm_client_numeric_facts.c.client_id == client_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if numeric is None:
        return None

    facts: dict[str, Any] = {
        key: bands_row[key] for key in _BAND_FACT_KEYS if bands_row.get(key) is not None
    }
    for key in _NUMERIC_FACT_KEYS:
        value = numeric.get(key)
        if value is not None:
            # Postgres returns a computed ratio as numeric, which would coerce
            # to int and silently truncate.
            facts[key] = float(value) if isinstance(value, Decimal) else value

    fund_name = FUND_DISPLAY_NAMES.get(bands_row.get("fund_type") or "")
    if fund_name is not None:
        facts["fund_name"] = fund_name
    return ModelFactBlock(**facts).to_dict()


def load_client_context(
    session: Session, client_id: int, product: str, *, at: date | None = None
) -> ClientContext:
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
    on = at or date.today()
    brief = load_angle(session, indicators.message_angle, on)
    return ClientContext(
        raw_context=dict(row),
        angle=indicators.message_angle,
        prompt_variant=indicators.prompt_variant,
        chunks=chunks,
        brief=brief,
        contract=load_tier(session, indicators.priority_tier, on),
        facts=load_client_facts(session, client_id, dict(row)),
        priority_tier=indicators.priority_tier,
        rule_version=indicators.rule_version,
        angle_catalog_version=brief.version if brief is not None else None,
        data_date=latest_reference_date(session),
    )


def _traced(
    name: str,
    fn: Callable[[GenerationState], dict[str, Any]],
    tracer: Tracer,
    *,
    as_type: str = "span",
    model: str | None = None,
):
    # Wrap a node so one Langfuse span covers its call, under the run's trace_id.

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
            trace_id=state["trace_id"],
            name=name,
            input=dict(state),
            metadata=metadata,
            as_type=as_type,
            model=model,
        )
        result = fn(state)
        usage_details = None
        if as_type == "generation":
            calls = result.get("llm_calls") or state.get("llm_calls") or []
            if calls and calls[-1]["input_tokens"] is not None:
                last = calls[-1]
                usage_details = {"input": last["input_tokens"], "output": last["output_tokens"]}
        tracer.end_span(handle, output=result, usage_details=usage_details)
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
        tool_calls = [
            {
                "tool_name": "context_fetch",
                "input": {"client_id": state["client_id"], "product": state["product"]},
                "output": {
                    "angle": client_context.angle,
                    "prompt_variant": client_context.prompt_variant,
                },
            },
            {
                "tool_name": "rag_retrieval",
                "input": {"product": state["product"], "angle": client_context.angle},
                "output": {
                    "chunk_count": len(client_context.chunks),
                    "chunks": [
                        {"chunk_id": c.chunk_id, "text": c.text} for c in client_context.chunks
                    ],
                },
            },
        ]
        return {
            "raw_context": client_context.raw_context,
            "angle": client_context.angle,
            "priority_tier": client_context.priority_tier,
            "prompt_variant": client_context.prompt_variant,
            "chunks": client_context.chunks,
            "brief": client_context.brief,
            "contract": client_context.contract,
            "facts": client_context.facts,
            "rule_version": client_context.rule_version,
            "angle_catalog_version": client_context.angle_catalog_version,
            "data_date": client_context.data_date,
            "tool_calls": tool_calls,
        }

    def assemble_prompt(state: GenerationState) -> dict[str, Any]:
        # The fact block is the payload when there is one, so the client's own
        # figures cross the boundary through the scanner rather than riding in
        # the prompt text, which is never scanned.
        facts = state.get("facts")
        context = dict(facts) if facts else to_model_context(state["raw_context"])
        prompt = prompt_builder(
            angle=state.get("angle"),
            prompt_variant=state.get("prompt_variant"),
            chunks=state.get("chunks", ()),
            brief=state.get("brief"),
            contract=state.get("contract"),
            facts=facts,
        )
        return {"context": context, "system_prompt": prompt}

    def _call_record(attempt: int, system_prompt: str, raw_output: str | None, latency_ms: int):
        usage = getattr(llm_client, "last_usage", None)
        return {
            "attempt": attempt,
            "system_prompt": system_prompt,
            "raw_output": raw_output,
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "latency_ms": latency_ms,
        }

    def generate(state: GenerationState) -> dict[str, Any]:
        attempts = state.get("attempts", 0) + 1
        model_call = as_model_call(llm_client, system=state["system_prompt"])
        started = time.monotonic()
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
            latency_ms = int((time.monotonic() - started) * 1000)
            calls = [
                *state.get("llm_calls", ()),
                _call_record(attempts, state["system_prompt"], None, latency_ms),
            ]
            return {
                "attempts": attempts,
                "llm_calls": calls,
                **_retry_or_reject(attempts, "pii_scan", str(leak)),
            }
        latency_ms = int((time.monotonic() - started) * 1000)
        calls = [
            *state.get("llm_calls", ()),
            _call_record(attempts, state["system_prompt"], draft, latency_ms),
        ]
        return {
            "draft": draft,
            "attempts": attempts,
            "llm_calls": calls,
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
            structured = parse_email_draft(state.get("draft") or "", state.get("facts"))
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
    graph.add_node(
        "generate",
        _traced("generate", generate, tracer, as_type="generation", model=llm_client.model),
    )
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
