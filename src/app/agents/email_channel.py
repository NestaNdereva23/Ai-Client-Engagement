from __future__ import annotations

import functools
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.agents.email_agent import build_system_prompt, render_call_brief
from app.agents.graph import (
    DEFAULT_MAX_ATTEMPTS,
    ConfigResolver,
    ContextLoader,
    GenerationState,
    GuardrailCheck,
    PromptBuilder,
    build_generation_graph,
    load_client_context,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS
from app.agents.prompt_config import resolve_active_configuration
from app.config import Settings, get_settings
from app.llmops.tracing import NullTracer, Tracer
from app.privacy.boundary import AuditSink
from app.privacy.llm_client import LLMClient, get_llm_client
from app.services.rag import get_rag_enabled

CHANNEL = "email"
CALL_BRIEF_CHANNEL = "call_brief"


def attach_call_brief(state: GenerationState) -> GenerationState:
    contract = state.get("contract")
    brief = state.get("brief")
    if state.get("status") != "accepted" or contract is None or brief is None:
        return state
    if getattr(contract, "secondary_channel", None) != CALL_BRIEF_CHANNEL:
        return state

    state["call_brief"] = render_call_brief(
        brief=brief, facts=state.get("facts") or {}, contract=contract
    )
    return state


class EmailAgent:
    channel = CHANNEL

    def __init__(
        self,
        *,
        context_loader: ContextLoader,
        llm_client: LLMClient,
        guardrail_checks: Sequence[GuardrailCheck] = DEFAULT_GUARDRAIL_CHECKS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        audit: AuditSink | None = None,
        tracer: Tracer | None = None,
        prompt_builder: PromptBuilder = build_system_prompt,
        config_resolver: ConfigResolver | None = None,
    ) -> None:
        self._tracer = tracer or NullTracer()
        self._graph = build_generation_graph(
            context_loader=context_loader,
            llm_client=llm_client,
            guardrail_checks=guardrail_checks,
            prompt_builder=prompt_builder,
            config_resolver=config_resolver,
            max_attempts=max_attempts,
            audit=audit,
            tracer=self._tracer,
        )

    def generate(self, *, client_id: int, product: str) -> GenerationState:
        state = new_generation_state(client_id=client_id, product=product)
        try:
            final = self._graph.invoke(state)
        finally:
            self._tracer.flush()
        return attach_call_brief(final)


def build_default_agent(
    session: Session,
    settings: Settings | None = None,
    *,
    audit: AuditSink | None = None,
    tracer: Tracer | None = None,
    prompt_builder: PromptBuilder = build_system_prompt,
) -> EmailAgent:
    settings = settings or get_settings()
    use_rag = settings.rag_enabled and get_rag_enabled(session)
    return EmailAgent(
        context_loader=functools.partial(load_client_context, session, use_rag=use_rag),
        llm_client=get_llm_client(settings),
        audit=audit,
        tracer=tracer,
        prompt_builder=prompt_builder,
        config_resolver=functools.partial(resolve_active_configuration, session),
    )
