"""EmailAgent: the email channel, registered behind the orchestrator.

Wraps the generation graph (agents.graph) and EmailAgent's own prompt logic
(agents.email_agent) behind the orchestrator.ChannelAgent shape: a channel
name plus one generate(client_id, product) method. This is module
wires the two together, so agents.graph never has to import
agents.email_agent's default prompt builder itself in a way that would stop
a different channel from reusing the graph machinery.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.email_agent import build_system_prompt, render_call_brief
from app.agents.graph import (
    DEFAULT_MAX_ATTEMPTS,
    ContextLoader,
    GenerationState,
    GuardrailCheck,
    build_generation_graph,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS
from app.llmops.tracing import NullTracer, Tracer
from app.privacy.boundary import AuditSink
from app.privacy.llm_client import LLMClient

CHANNEL = "email"
# What a tier contract names when its tier gets a brief as well as an email.
CALL_BRIEF_CHANNEL = "call_brief"


def attach_call_brief(state: GenerationState) -> GenerationState:
    """Render the accompanying call brief for a tier whose contract adds one.

    A second render of the draft that was already accepted, from the same
    angle brief and the same facts, so the brief and the email cannot tell
    the client two different stories.
    """
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
    """The email channel agent: channel_id "email", draft generation via agents.graph."""

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
    ) -> None:
        self._tracer = tracer or NullTracer()
        self._graph = build_generation_graph(
            context_loader=context_loader,
            llm_client=llm_client,
            guardrail_checks=guardrail_checks,
            prompt_builder=build_system_prompt,
            max_attempts=max_attempts,
            audit=audit,
            tracer=self._tracer,
        )

    def generate(self, *, client_id: int, product: str) -> GenerationState:
        """Run the graph for one client's draft and return the terminal state."""
        state = new_generation_state(client_id=client_id, product=product)
        try:
            final = self._graph.invoke(state)
        finally:
            self._tracer.flush()
        return attach_call_brief(final)
