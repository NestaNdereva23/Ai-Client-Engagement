"""The top tier's call brief, rendered alongside its email.

One generation, two renders. The brief exists only for a tier whose contract
asks for one, only once the email it accompanies was accepted, and it is
built from the same angle brief and the same facts so the two cannot tell
the client different stories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.agents.email_channel import EmailAgent, attach_call_brief
from app.agents.graph import ClientContext


@dataclass(frozen=True)
class FakeBrief:
    headline: str = "Restart the standing order"
    who: str = "Five or more purchases on a tight cadence"
    claim: str = "A genuine, measurable savings rhythm that stopped"
    ask: str = "Resume the exact cadence they already had"
    never: str = "Never state an exact purchase count"
    use: str = "Reference the cadence placeholder if given; support with product info"


@dataclass(frozen=True)
class FakeContract:
    max_words: int = 120
    sign_off: str = "named relationship manager"
    secondary_channel: str | None = None


FACTS = {
    "fund_name": "Cytonn Money Market Fund",
    "typical_contribution_kes": 150_000,
    "invested_every_n_days": 30,
    "cadence_band": "Tight",
}


def _state(**overrides):
    state = {
        "status": "accepted",
        "brief": FakeBrief(),
        "contract": FakeContract(secondary_channel="call_brief"),
        "facts": FACTS,
        "subject": "Come back",
        "body": "Dear {{first_name}}, welcome back.",
    }
    state.update(overrides)
    return state


def test_the_top_tier_gets_a_brief_alongside_its_email() -> None:
    state = attach_call_brief(_state())
    assert state.get("call_brief")
    # The email is untouched: one generation, two renders of it.
    assert state["body"] == "Dear {{first_name}}, welcome back."


def test_a_tier_whose_contract_adds_nothing_gets_no_brief() -> None:
    state = attach_call_brief(_state(contract=FakeContract(secondary_channel=None)))
    assert state.get("call_brief") is None


def test_a_rejected_draft_gets_no_brief() -> None:
    """Nothing was accepted, so there is no message for a brief to accompany."""
    state = attach_call_brief(_state(status="rejected"))
    assert state.get("call_brief") is None


def test_a_run_with_no_contract_gets_no_brief() -> None:
    state = attach_call_brief(_state(contract=None))
    assert state.get("call_brief") is None


def test_a_run_with_no_angle_brief_gets_no_brief() -> None:
    state = attach_call_brief(_state(brief=None))
    assert state.get("call_brief") is None


# --- through the real channel agent ---


class ScriptedLLMClient:
    model = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.last_usage = None

    def generate(self, *, system: str, user: str) -> str:
        return self._replies.pop(0)


_BRIEF = FakeBrief()


def _loader(contract, brief=_BRIEF, facts=FACTS):
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={},
            angle="back_on_schedule",
            prompt_variant="back_on_schedule",
            chunks=(),
            brief=brief,
            contract=contract,
            facts=facts,
        )

    return load


def _draft() -> str:
    return json.dumps(
        {
            "subject": "Restart your standing order",
            "body": (
                "Dear {{first_name}}, you were putting money into your "
                "Cytonn Money Market Fund every 30 days, usually around "
                "KES 150,000. Restarting takes about two minutes.\n\n"
                "Best regards, named relationship manager"
            ),
        }
    )


def test_the_channel_agent_attaches_a_brief_for_the_top_tier() -> None:
    agent = EmailAgent(
        context_loader=_loader(FakeContract(secondary_channel="call_brief")),
        llm_client=ScriptedLLMClient([_draft()]),
    )
    state = agent.generate(client_id=1, product="money market")
    assert state["status"] == "accepted"
    assert "Restart the standing order" in state["call_brief"]
    assert "named relationship manager" in state["call_brief"]


def test_the_channel_agent_attaches_no_brief_for_a_lower_tier() -> None:
    agent = EmailAgent(
        context_loader=_loader(FakeContract(secondary_channel=None)),
        llm_client=ScriptedLLMClient([_draft()]),
    )
    state = agent.generate(client_id=1, product="money market")
    assert state["status"] == "accepted"
    assert state.get("call_brief") is None
