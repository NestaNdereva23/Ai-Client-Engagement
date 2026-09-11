"""The wrappers that put a model call and a tool call on the trace.

These prove the thing that makes a cost appear next to a run: every turn
becomes a generation span carrying the model name and the token counts it
actually used. They also prove a refusal and a failure are marked, and
that the tally counts turns rather than steps.
"""

from __future__ import annotations

import pytest

from app.llmops.spans import (
    ModelCallTally,
    counting_converse,
    traced_aconverse,
    traced_async_tool_call,
    traced_converse,
    traced_tool_call,
)
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest

USAGE = LLMUsage(input_tokens=120, output_tokens=45)


class RecordingTracer:
    """Keeps every span instead of sending it anywhere."""

    def __init__(self) -> None:
        self.spans: list[dict] = []

    def start_span(
        self, *, trace_id, name, input, metadata=None, as_type="span", model=None, parent=None
    ):
        span = {
            "trace_id": trace_id,
            "name": name,
            "input": input,
            "metadata": metadata,
            "as_type": as_type,
            "model": model,
            "parent": parent,
            "output": None,
            "usage_details": None,
            "level": None,
            "status_message": None,
        }
        self.spans.append(span)
        return span

    def end_span(self, handle, *, output, usage_details=None, level=None, status_message=None):
        handle["output"] = output
        handle["usage_details"] = usage_details
        handle["level"] = level
        handle["status_message"] = status_message


def a_turn(text: str = "", tool_name: str | None = None) -> ConversationTurn:
    requests = (
        ()
        if tool_name is None
        else (ToolUseRequest(call_id="c1", tool_name=tool_name, tool_input={"a": 1}),)
    )
    return ConversationTurn(
        text=text,
        tool_requests=requests,
        usage=USAGE,
        stop_reason="tool_use" if tool_name else "end_turn",
    )


def test_a_model_call_becomes_a_generation_span_with_its_model_and_tokens() -> None:
    tracer = RecordingTracer()
    call = traced_converse(
        lambda messages: a_turn(tool_name="list_groups"),
        tracer=tracer,
        trace_id="a" * 32,
        model="claude-opus-5",
        system="you look for things",
        metadata={"group_name": "fees_will_empty"},
    )

    turn = call([{"role": "user", "content": "start"}])

    assert turn.tool_requests[0].tool_name == "list_groups"
    span = tracer.spans[0]
    assert span["as_type"] == "generation"
    assert span["model"] == "claude-opus-5"
    assert span["usage_details"] == {"input": 120, "output": 45}
    assert span["input"] == {
        "system": "you look for things",
        "messages": [{"role": "user", "content": "start"}],
    }
    assert span["output"]["tool_calls"] == [{"tool_name": "list_groups", "tool_input": {"a": 1}}]
    assert span["metadata"] == {"group_name": "fees_will_empty"}


async def test_the_async_model_call_traces_exactly_the_same_way() -> None:
    tracer = RecordingTracer()

    async def converse(messages):
        return a_turn(text="done")

    call = traced_aconverse(
        converse, tracer=tracer, trace_id="a" * 32, model="claude-opus-5", system="s"
    )

    await call([])

    span = tracer.spans[0]
    assert span["as_type"] == "generation"
    assert span["usage_details"] == {"input": 120, "output": 45}
    assert span["output"]["text"] == "done"


def test_a_model_call_that_fails_is_marked_and_still_raises() -> None:
    tracer = RecordingTracer()

    def converse(messages):
        raise RuntimeError("the model server is unreachable")

    call = traced_converse(converse, tracer=tracer, trace_id="a" * 32, model="m", system="s")

    with pytest.raises(RuntimeError):
        call([])

    span = tracer.spans[0]
    assert span["level"] == "ERROR"
    assert span["status_message"] == "the model server is unreachable"


def test_a_tool_call_becomes_its_own_span_under_the_step_it_happened_in() -> None:
    tracer = RecordingTracer()
    parent = {"name": "fees_will_empty"}
    call = traced_tool_call(
        lambda name, arguments: {"client_count": 4},
        tracer=tracer,
        trace_id="a" * 32,
        parent=parent,
    )

    output = call("measure_slice", {"conditions": []})

    assert output == {"client_count": 4}
    span = tracer.spans[0]
    assert span["name"] == "measure_slice"
    assert span["as_type"] == "tool"
    assert span["parent"] is parent
    assert span["input"] == {"conditions": []}
    assert span["level"] is None


def test_a_tool_that_refuses_is_marked_as_a_warning() -> None:
    tracer = RecordingTracer()
    call = traced_tool_call(
        lambda name, arguments: {"error": "unknown_kind", "message": "no"},
        tracer=tracer,
        trace_id="a" * 32,
    )

    call("write_insight", {})

    assert tracer.spans[0]["level"] == "WARNING"


async def test_the_async_tool_wrapper_marks_a_refusal_the_same_way() -> None:
    tracer = RecordingTracer()

    async def call_tool(name, arguments):
        return {"error": "budget_spent", "message": "no queries left"}

    call = traced_async_tool_call(call_tool, tracer=tracer, trace_id="a" * 32)

    await call("measure_slice", {})

    assert tracer.spans[0]["level"] == "WARNING"


def test_the_tally_counts_turns_and_adds_up_the_tokens() -> None:
    tally = ModelCallTally()
    call = counting_converse(lambda messages: a_turn(text="hello"), tally)

    call([])
    call([])

    assert tally.as_detail() == {"model_calls": 2, "input_tokens": 240, "output_tokens": 90}
