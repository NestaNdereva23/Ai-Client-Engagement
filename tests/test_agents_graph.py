"""The generation graph: retrieve, assemble, generate, guardrails, no send.

These prove the four nodes run in order, run_id/trace_id are set once and
carried through every node (and into the boundary audit), the model's raw
output is parsed and validated against the EmailDraft schema before any other
guardrail runs, an outbound PII leak and an ungrounded or malformed draft are
all handled as retryable output guardrails with the failing one recorded on
GenerationState.failed_guardrail, a structurally or semantically bad draft is
rejected once the retry budget runs out, and a leak at the privacy boundary
from a broken payload (inbound) still aborts the whole run rather than being
swallowed as a retryable guardrail failure. There is never a path out of this
graph to anything resembling a send.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.agents.email_agent import build_system_prompt
from app.agents.graph import (
    ClientContext,
    GenerationState,
    build_generation_graph,
    new_generation_state,
)
from app.agents.guardrails import GuardrailFailure
from app.privacy.boundary import BoundaryAudit
from app.privacy.scanners import InboundLeak

RAW_CONTEXT = {
    "client_id": 1001,
    "archetype": "One-and-done",
    "recency_bucket": "Exited 3y plus",
    "value_tier_label": "High",
    "rhythm_band": "Unknown",
}


def draft_json(subject: str = "Come back to {{fund_name}}", body: str = "") -> str:
    """A raw model output: valid JSON matching the EmailDraft schema."""
    return json.dumps({"subject": subject, "body": body})


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
    llm = ScriptedLLMClient(
        [draft_json(body="Dear {{first_name}}, your {{fund_name}} returned 11.35% last week.")]
    )
    graph = build_generation_graph(context_loader=make_context_loader(chunks), llm_client=llm)

    state = new_generation_state(client_id=1001, product="money market")
    result = graph.invoke(state)

    assert result["status"] == "accepted"
    assert result["subject"] == "Come back to {{fund_name}}"
    assert result["body"] == "Dear {{first_name}}, your {{fund_name}} returned 11.35% last week."
    assert result["raw_structured_output"] == {
        "subject": result["subject"],
        "body": result["body"],
    }
    assert result["attempts"] == 1
    # run_id/trace_id set once at entry, unchanged by every node.
    assert result["run_id"] == state["run_id"]
    assert result["trace_id"] == state["trace_id"]
    # The angle and facts reached the model only via the system prompt, never
    # as extra keys in the boundary-scanned context.
    assert llm.calls[0]["system"].count("11.35%") == 1
    assert "winback_habit" in llm.calls[0]["system"]

    assert len(result["llm_calls"]) == 1
    call = result["llm_calls"][0]
    assert call["attempt"] == 1
    assert call["raw_output"] == result["draft"]
    assert isinstance(call["latency_ms"], int)

    tool_names = {call["tool_name"] for call in result["tool_calls"]}
    assert tool_names == {"context_fetch", "rag_retrieval"}
    rag_call = next(c for c in result["tool_calls"] if c["tool_name"] == "rag_retrieval")
    assert rag_call["output"]["chunk_count"] == 1
    assert rag_call["output"]["chunks"] == [
        {"chunk_id": 1, "text": "the fund yielded 11.35% this week"}
    ]


def test_llm_calls_gets_one_entry_per_attempt_on_a_retry() -> None:
    llm = ScriptedLLMClient(
        [
            draft_json(body="Dear {{first_name}}, call 0712345678 about {{fund_name}}."),
            draft_json(body="Dear {{first_name}}, welcome back to {{fund_name}}."),
        ]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=2
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "accepted"
    assert [call["attempt"] for call in result["llm_calls"]] == [1, 2]
    assert result["llm_calls"][0]["raw_output"] is None
    assert result["llm_calls"][1]["raw_output"] == result["draft"]


class SpyTracer:
    """Records start_span/end_span calls in order; never talks to Langfuse."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.ended: list[dict] = []

    def start_span(self, *, trace_id, name, input, metadata=None, as_type="span", model=None):
        handle = object()
        self.started.append(
            {
                "trace_id": trace_id,
                "name": name,
                "input": input,
                "metadata": metadata,
                "as_type": as_type,
                "model": model,
            }
        )
        return handle

    def end_span(self, handle, *, output, usage_details=None) -> None:
        self.ended.append({"handle": handle, "output": output, "usage_details": usage_details})

    def get_trace_url(self, trace_id: str) -> None:
        return None

    def flush(self) -> None:
        pass


def test_tracer_records_one_span_per_node_under_the_runs_trace_id() -> None:
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")])
    tracer = SpyTracer()
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, tracer=tracer
    )

    state = new_generation_state(client_id=1001, product="money market")
    result = graph.invoke(state)

    assert result["status"] == "accepted"
    # trace_id is a bare 32-char hex id: valid as-is as a Langfuse trace id.
    assert len(state["trace_id"]) == 32
    assert all(c in "0123456789abcdef" for c in state["trace_id"])

    node_names = [call["name"] for call in tracer.started]
    assert node_names == ["retrieve_context", "assemble_prompt", "generate", "guardrails"]
    assert all(call["trace_id"] == state["trace_id"] for call in tracer.started)
    assert len(tracer.ended) == len(tracer.started)

    first_metadata = tracer.started[0]["metadata"]
    assert first_metadata == {
        "run_id": state["run_id"],
        "client_id": state["client_id"],
        "product": state["product"],
    }
    assert all(call["metadata"] is None for call in tracer.started[1:])

    # Only "generate" is tagged as a Langfuse generation; that is what makes
    # Langfuse compute token usage and cost for it.
    as_types = {call["name"]: call["as_type"] for call in tracer.started}
    assert as_types["generate"] == "generation"
    assert as_types["retrieve_context"] == "span"
    generate_start = next(c for c in tracer.started if c["name"] == "generate")
    assert generate_start["model"] == "stub"


def test_tracer_attaches_token_usage_only_to_the_generate_span() -> None:
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")])
    llm.last_usage = type("Usage", (), {"input_tokens": 11, "output_tokens": 22})()
    tracer = SpyTracer()
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, tracer=tracer
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    node_names = [call["name"] for call in tracer.started]
    generate_index = node_names.index("generate")
    assert tracer.ended[generate_index]["usage_details"] == {"input": 11, "output": 22}
    other_indices = [i for i in range(len(node_names)) if i != generate_index]
    assert all(tracer.ended[i]["usage_details"] is None for i in other_indices)
    assert result["llm_calls"][0]["input_tokens"] == 11
    assert result["llm_calls"][0]["output_tokens"] == 22


def test_default_prompt_builder_is_email_agents_and_reflects_the_rule_outcome() -> None:
    """prompt_variant comes from the rule outcome (M4), not a hard-coded template."""
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")])
    graph = build_generation_graph(context_loader=make_context_loader(), llm_client=llm)

    graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert llm.calls[0]["system"] == build_system_prompt(
        angle="winback_habit", prompt_variant="habit_premium"
    )


def test_prompt_builder_is_injectable_for_a_future_channel() -> None:
    """A future channel can swap EmailAgent's prompt builder without touching the graph."""
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")])
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
            return draft_json(body="Dear {{first_name}}, welcome back.")

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
            draft_json(body="Dear {{first_name}}, we made up a 99.99% return for {{fund_name}}."),
            draft_json(body="Dear {{first_name}}, {{fund_name}} returned 11.35% last week."),
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
            draft_json(body="Dear {{first_name}}, we made up a 99.99% return for {{fund_name}}."),
            draft_json(
                body="Dear {{first_name}}, still making up a 42.00% return for {{fund_name}}."
            ),
        ]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=2
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["attempts"] == 2
    assert result["failed_guardrail"] == "grounding"
    # The reason reflects the last draft tried, not the first.
    assert "42.00" in (result["reason"] or "")
    assert len(llm.calls) == 2


def test_never_reaches_accepted_without_passing_every_guardrail_check() -> None:
    """A custom check can fail forever; the run still stops at rejected, not send."""

    def always_fails(state: GenerationState) -> None:
        raise GuardrailFailure("never good enough")

    llm = ScriptedLLMClient(
        [
            draft_json(body="Dear {{first_name}}, draft one for {{fund_name}}."),
            draft_json(body="Dear {{first_name}}, draft two for {{fund_name}}."),
        ]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(),
        llm_client=llm,
        guardrail_checks=(always_fails,),
        max_attempts=2,
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["reason"] == "never good enough"
    # A GuardrailFailure raised without a name still gets recorded on the trace.
    assert result["failed_guardrail"] == "unknown"


def test_malformed_json_draft_retries_then_is_rejected_after_the_budget() -> None:
    """A draft that is not valid JSON never reaches the pluggable checks at all."""
    llm = ScriptedLLMClient(["not json at all", "still not json"])
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=2
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["attempts"] == 2
    assert result["failed_guardrail"] == "structured_output"
    assert "not valid JSON" in (result["reason"] or "")
    # A draft that never parsed has no validated body or structured output.
    assert result.get("body") is None
    assert result.get("raw_structured_output") is None


def test_draft_missing_a_required_placeholder_is_rejected() -> None:
    llm = ScriptedLLMClient(
        [draft_json(subject="Welcome back", body="Dear {{first_name}}, welcome back.")]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=1
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["failed_guardrail"] == "structured_output"
    assert "missing required placeholders" in (result["reason"] or "")
    assert "{{fund_name}}" in (result["reason"] or "")


def test_draft_with_an_unexpected_placeholder_token_is_rejected() -> None:
    llm = ScriptedLLMClient(
        [draft_json(body="Dear {{first_name}}, your {{fund_name}} made {{amount}}.")]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=1
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["failed_guardrail"] == "structured_output"
    assert "unexpected placeholder" in (result["reason"] or "")
    assert "{{amount}}" in (result["reason"] or "")


def test_a_format_violation_is_rejected_and_recorded_as_format_length() -> None:
    """A body that is too short passes structured output and grounding, then fails format."""
    llm = ScriptedLLMClient([draft_json(subject="Hi {{first_name}}", body="{{fund_name}}")])
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=1
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["failed_guardrail"] == "format_length"
    assert "under" in (result["reason"] or "")
    # Structured output did validate; the failure is purely about length.
    assert result["body"] == "{{fund_name}}"


def test_an_outbound_pii_leak_retries_then_accepts_a_clean_draft() -> None:
    """The model's own words leaking a contact channel is a retryable output guardrail."""
    llm = ScriptedLLMClient(
        [
            draft_json(
                body="Dear {{first_name}}, reach us at winback@example.com re {{fund_name}}."
            ),
            draft_json(body="Dear {{first_name}}, welcome back to {{fund_name}}."),
        ]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=2
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "accepted"
    assert result["attempts"] == 2
    assert len(llm.calls) == 2


def test_a_persistent_outbound_pii_leak_is_rejected_and_recorded_as_pii_scan() -> None:
    llm = ScriptedLLMClient(
        [
            draft_json(body="Dear {{first_name}}, call 0712345678 about {{fund_name}}."),
            draft_json(body="Dear {{first_name}}, email winback@example.com re {{fund_name}}."),
        ]
    )
    graph = build_generation_graph(
        context_loader=make_context_loader(), llm_client=llm, max_attempts=2
    )

    result = graph.invoke(new_generation_state(client_id=1001, product="money market"))

    assert result["status"] == "rejected"
    assert result["attempts"] == 2
    assert result["failed_guardrail"] == "pii_scan"
    # The leaked draft never reached structured-output parsing.
    assert result.get("body") is None
    assert result.get("raw_structured_output") is None
    assert len(llm.calls) == 2


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
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, welcome back.")])
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
