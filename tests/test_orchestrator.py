"""The orchestrator: channel-agnostic routing to a registered channel agent.

These prove registering and generating on a channel routes to the right
agent and no other, an unregistered channel raises rather than guessing,
EmailAgent satisfies the ChannelAgent shape and can be driven through the
orchestrator exactly like any other channel, and the orchestrator module
itself never imports anything email-specific, so a future channel never has
to touch this file to slot in.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field

import pytest

import app.agents.orchestrator as orchestrator_module
from app.agents.email_channel import CHANNEL as EMAIL_CHANNEL
from app.agents.email_channel import EmailAgent
from app.agents.graph import ClientContext, GenerationState
from app.agents.orchestrator import ChannelAgent, Orchestrator, UnknownChannel

RAW_CONTEXT = {
    "client_id": 1001,
    "archetype": "One-and-done",
    "recency_bucket": "Exited 3y plus",
    "value_tier_label": "High",
    "rhythm_band": "Unknown",
}


@dataclass
class FakeChannelAgent:
    channel: str
    calls: list[dict] = field(default_factory=list)

    def generate(self, *, client_id: int, product: str) -> GenerationState:
        self.calls.append({"client_id": client_id, "product": product})
        return {"status": "accepted", "client_id": client_id, "product": product}


def test_register_and_generate_routes_to_the_matching_agent_only() -> None:
    email = FakeChannelAgent(channel="email")
    sms = FakeChannelAgent(channel="sms")
    orchestrator = Orchestrator()
    orchestrator.register(email)
    orchestrator.register(sms)

    result = orchestrator.generate("sms", client_id=1001, product="money market")

    assert result["status"] == "accepted"
    assert sms.calls == [{"client_id": 1001, "product": "money market"}]
    assert email.calls == []


def test_channels_lists_every_registered_channel() -> None:
    orchestrator = Orchestrator()
    orchestrator.register(FakeChannelAgent(channel="email"))
    orchestrator.register(FakeChannelAgent(channel="sms"))
    assert set(orchestrator.channels()) == {"email", "sms"}


def test_generate_on_an_unregistered_channel_raises_unknown_channel() -> None:
    orchestrator = Orchestrator()
    orchestrator.register(FakeChannelAgent(channel="email"))
    with pytest.raises(UnknownChannel, match="whatsapp"):
        orchestrator.generate("whatsapp", client_id=1001, product="money market")


def test_a_fake_channel_agent_satisfies_the_protocol_via_duck_typing() -> None:
    assert isinstance(FakeChannelAgent(channel="email"), ChannelAgent)


def test_orchestrator_module_never_imports_anything_email_specific() -> None:
    """No email specifics leak into the seam: a future channel never touches this file."""
    source = inspect.getsource(orchestrator_module)
    leak = re.compile(r"^\s*(import|from)\s+app\.(agents\.email|schemas\.email)", re.MULTILINE)
    assert not leak.search(source)


def test_email_agent_declares_the_email_channel() -> None:
    assert EmailAgent.channel == "email"
    assert EMAIL_CHANNEL == "email"


class ScriptedLLMClient:
    """Returns each draft in order, one per generate() call. Mirrors test_agents_graph.py."""

    model = "stub"

    def __init__(self, drafts: list[str]) -> None:
        self._drafts = list(drafts)

    def generate(self, *, system: str, user: str) -> str:
        return self._drafts.pop(0)


def draft_json(subject: str = "Come back to {{fund_name}}", body: str = "") -> str:
    return json.dumps({"subject": subject, "body": body})


def make_context_loader():
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context=RAW_CONTEXT,
            angle="winback_habit",
            prompt_variant="habit_premium",
            chunks=(),
        )

    return load


def test_email_agent_satisfies_the_channel_agent_protocol() -> None:
    agent = EmailAgent(
        context_loader=make_context_loader(),
        llm_client=ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")]),
    )
    assert isinstance(agent, ChannelAgent)


def test_email_agent_registered_behind_the_orchestrator_produces_an_accepted_draft() -> None:
    agent = EmailAgent(
        context_loader=make_context_loader(),
        llm_client=ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")]),
    )
    orchestrator = Orchestrator()
    orchestrator.register(agent)

    result = orchestrator.generate("email", client_id=1001, product="money market")

    assert result["status"] == "accepted"
    assert result["body"] == "Dear {{first_name}}, welcome back."
    assert result["client_id"] == 1001
