"""The generation graph: retrieve, assemble, generate, guardrails, no send.

These prove the four nodes run in order, run_id/trace_id are set once and
carried through every node (and into the boundary audit), an ungrounded
draft retries then is regenerated, a draft that never grounds is rejected
after the retry budget, and a leak at the privacy boundary aborts the whole
run rather than being swallowed as a retryable guardrail failure. There is
never a path out of this graph to anything resembling a send.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agents.email_agent import build_system_prompt
from app.agents.graph import (
    ClientContext,
    GenerationState,
    GuardrailFailure,
    build_generation_graph,
    new_generation_state,
)
from app.privacy.boundary import BoundaryAudit
from app.privacy.scanners import InboundLeak

RAW_CONTEXT = {
    "client_id": 1001,
    "archetype": "One-and-done",
    "recency_bucket": "Exited 3y plus",
    "value_tier_label": "High",
    "rhythm_band": "Unknown",
}


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def make_context_loader(chunks=()):
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context=RAW_CONTEXT,
            angle="winback_habit",
            prompt_variant="habit_premium",
            chunks=chunks,
        )

    return load


class ScriptedLLMClient:
    """Returns each draft in order, one per generate() call."""

    model = "stub"

    def __init__(self, drafts: list[str]) -> None:
        self._drafts = list(drafts)
        self.calls: list[dict] = []

    def generate(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self._drafts.pop(0)


def test_happy_path_runs_all_four_nodes_and_carries_run_and_trace_ids() -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    llm = ScriptedLLMClient(["Dear {{first_name}}, your fund returned 11.35% last week."])
    graph = build_generation_graph(context_loader=make_context_loader(chunks), llm_client=llm)

    state = new_generation_state(client_id=1001, product="money market")
    result = graph.invoke(state)

    assert result["status"] == "accepted"
    assert result["draft"] == "Dear {{first_name}}, your fund returned 11.35% last week."
    assert result["attempts"] == 1
    # run_id/trace_id set once at entry, unchanged by every node.
    assert result["run_id"] == state["run_id"]
    assert result["trace_id"] == state["trace_id"]
    # The angle and facts reached the model only via the system prompt, never
    # as extra keys in the boundary-scanned context.
    assert llm.calls[0]["system"].count("11.35%") == 1
    assert "winback_habit" in llm.calls[0]["system"]


def test_default_prompt_builder_is_email_agents_and_reflects_the_rule_outcome() -> None:
    """prompt_variant comes from the rule outcome (M4), not a hard-coded template."""
    llm = ScriptedLLMClient(["Dear {{first_name}}, welcome back."])
    graph = build_generation_graph(context_loader=make_context_loader(), llm_client=llm)

    graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert llm.calls[0]["system"] == build_system_prompt(
        angle="winback_habit", prompt_variant="habit_premium"
    )


def test_prompt_builder_is_injectable_for_a_future_channel() -> None:
    """A future channel can swap EmailAgent's prompt builder without touching the graph."""
    llm = ScriptedLLMClient(["Dear {{first_name}}, welcome back."])
    graph = build_generation_graph(
        context_loader=make_context_loader(),
        llm_client=llm,
        prompt_builder=lambda **_: "a completely different channel's prompt",
    )

    graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert llm.calls[0]["system"] == "a completely different channel's prompt"


def test_boundary_context_carries_only_the_allowlisted_tiers() -> None:
    seen_payloads: list[dict] = []

    class RecordingLLMClient:
        model = "stub"

        def generate(self, *, system: str, user: str) -> str:
            seen_payloads.append(dict(item.split(": ", 1) for item in user.split("\n")))
            return "Dear {{first_name}}, welcome back."

    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=RecordingLLMClient()
    )
    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "accepted"
    assert seen_payloads[0] == {
        "archetype": "One-and-done",
        "recency_bucket": "Exited 3y plus",
        "value_tier_label": "High",
        "rhythm_band": "Unknown",
    }


def test_ungrounded_draft_retries_then_accepts_a_grounded_one() -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    llm = ScriptedLLMClient(
        [
            "Dear {{first_name}}, we made up a 99.99% return for you.",
            "Dear {{first_name}}, your fund returned 11.35% last week.",
        ]
    )
    graph = build_generation_graph(context_loader=make_context_loader(chunks), llm_client=llm)

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "accepted"
    assert result["attempts"] == 2
    assert len(llm.calls) == 2


def test_persistently_ungrounded_draft_is_rejected_after_the_retry_budget() -> None:
    llm = ScriptedLLMClient(
        [
            "Dear {{first_name}}, we made up a 99.99% return for you.",
            "Dear {{first_name}}, still making up a 42.00% return.",
        ]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=2
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["attempts"] == 2
    # The reason reflects the last draft tried, not the first.
    assert "42.00" in (result["reason"] or "")
    assert len(llm.calls) == 2


def test_never_reaches_accepted_without_passing_every_guardrail_check() -> None:
    """A custom check can fail forever; the run still stops at rejected, not send."""

    def always_fails(state: GenerationState) -> None:
        raise GuardrailFailure("never good enough")

    llm = ScriptedLLMClient(["draft one", "draft two"])
    graph = build_generation_graph(
        context_loader=make_context_loader(),
        llm_client=llm,
        guardrail_checks=(always_fails,),
        max_attempts=2,
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["reason"] == "never good enough"


def test_a_boundary_leak_aborts_the_run_instead_of_being_treated_as_retryable() -> None:
    class LeakyContextLoader:
        def __call__(self, client_id: int, product: str) -> ClientContext:
            # An allow-listed key with a value that looks like real contact
            # data: to_model_context keeps the key (it's allow-listed), so
            # this can only be caught by the pattern scan on values, which is
            # exactly what must abort the run before the model is called.
            return ClientContext(
                raw_context={**RAW_CONTEXT, "archetype": "reachable at jane@example.com"},
                angle="winback_habit",
                prompt_variant="habit_premium",
                chunks=(),
            )

    class NeverCalledLLMClient:
        model = "stub"

        def generate(self, *, system: str, user: str) -> str:
            raise AssertionError("the model must never be called after an inbound leak")

    graph = build_generation_graph(
        context_loader=LeakyContextLoader(), llm_client=NeverCalledLLMClient()
    )

    with pytest.raises(InboundLeak):
        graph.invoke(new_generation_state(client_id=1001, product="money market"))


def test_boundary_audit_sink_receives_the_runs_ids() -> None:
    records: list[BoundaryAudit] = []
    llm = ScriptedLLMClient(["Dear {{first_name}}, welcome back."])
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, audit=records.append
    )

    state = new_generation_state(client_id=1001, product="money market")
    graph.invoke(state)

    assert len(records) == 1
    assert records[0].run_id == state["run_id"]
    assert records[0].trace_id == state["trace_id"]
    assert records[0].entity_id == "1001"
    assert records[0].inbound == "pass"
    assert records[0].outbound == "pass"


def test_graph_has_no_edge_resembling_a_send() -> None:
    """The compiled graph's node set is exactly the four M6.2 nodes, nothing else."""
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=ScriptedLLMClient([])
    )
    nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"retrieve_context", "assemble_prompt", "generate", "guardrails"}
