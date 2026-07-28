"""EmailAgent: the email channel, registered behind the orchestrator.

Wraps the generation graph (agents.graph) and EmailAgent's own prompt logic
(agents.email_agent) behind the orchestrator.ChannelAgent shape: a channel
name plus one generate(client_id, product) method. This is the only module
that wires the two together, so agents.graph never has to import
agents.email_agent's default prompt builder itself in a way that would stop
a different channel from reusing the graph machinery.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.email_agent import build_system_prompt
from app.agents.graph import (
    DEFAULT_MAX_ATTEMPTS,
    ContextLoader,
    GenerationState,
    GuardrailCheck,
    build_generation_graph,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS
from app.privacy.boundary import AuditSink
from app.privacy.llm_client import LLMClient

CHANNEL = "email"


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
    ) -> None:
        self._graph = build_generation_graph(
            context_loader=context_loader,
            llm_client=llm_client,
            guardrail_checks=guardrail_checks,
            prompt_builder=build_system_prompt,
            max_attempts=max_attempts,
            audit=audit,
        )

    def generate(self, *, client_id: int, product: str) -> GenerationState:
        """Run the graph for one client's draft and return the terminal state."""
        state = new_generation_state(client_id=client_id, product=product)
        return self._graph.invoke(state)
