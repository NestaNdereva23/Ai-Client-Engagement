"""Channel-agnostic orchestrator that routes work to a channel agent.

The orchestrator holds no channel-specific knowledge: it only ever calls
agent.generate(client_id=..., product=...) on whatever ChannelAgent was
registered under a channel name. Email drafting lives entirely behind that
one method on the registered agent (agents.email_channel.EmailAgent); this
module never imports it or anything email-specific, so a future SMS or
WhatsApp agent slots in by registering, with no change here. M9's campaign
steps call this seam, not a channel agent directly, so they stay
channel-neutral too.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.agents.graph import GenerationState


@runtime_checkable
class ChannelAgent(Protocol):
    """One channel's draft-generation entry point, the same shape for every channel."""

    channel: str

    def generate(self, *, client_id: int, product: str) -> GenerationState: ...


class UnknownChannel(KeyError):
    """Raised when asked to generate on a channel no agent is registered for."""


class Orchestrator:
    """Routes a generation request to whichever channel agent is registered for it."""

    def __init__(self) -> None:
        self._agents: dict[str, ChannelAgent] = {}

    def register(self, agent: ChannelAgent) -> None:
        """Register a channel agent under its own declared channel name."""
        self._agents[agent.channel] = agent

    def channels(self) -> tuple[str, ...]:
        """The channel names currently registered."""
        return tuple(self._agents)

    def generate(self, channel: str, *, client_id: int, product: str) -> GenerationState:
        """Delegate to the agent registered for channel, or raise UnknownChannel."""
        try:
            agent = self._agents[channel]
        except KeyError:
            raise UnknownChannel(f"no channel agent registered for {channel!r}") from None
        return agent.generate(client_id=client_id, product=product)
