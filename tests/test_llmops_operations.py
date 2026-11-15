from __future__ import annotations

import pytest

from app.llmops.operations import active_tracer, trace_operation, traced_model_call
from app.llmops.tracing import NullTracer


class RecordingTracer:
    def __init__(self) -> None:
        self.spans: list[dict] = []
        self.flushes = 0

    def start_span(
        self, *, trace_id, name, input, metadata=None, as_type="span", model=None, parent=None
    ):
        span = {
            "trace_id": trace_id,
            "name": name,
            "input": input,
            "as_type": as_type,
            "model": model,
            "parent": parent,
            "output": None,
            "level": None,
        }
        self.spans.append(span)
        return span

    def end_span(self, handle, *, output, usage_details=None, level=None, status_message=None):
        handle["output"] = output
        handle["level"] = level

    def flush(self) -> None:
        self.flushes += 1


class FakeClient:
    model = "claude-test"


def test_the_outer_operation_opens_a_trace_and_inner_ones_nest_inside_it() -> None:
    tracer = RecordingTracer()

    with trace_operation("Outer", tracer=tracer) as outer:
        outer.output = {"done": True}
        with trace_operation("Inner") as inner:
            inner.output = {"step": 1}

    outer_span, inner_span = tracer.spans
    assert inner_span["trace_id"] == outer_span["trace_id"]
    assert inner_span["parent"] is outer_span
    assert outer_span["parent"] is None
    assert outer_span["output"] == {"done": True}
    assert inner_span["output"] == {"step": 1}
    assert tracer.flushes == 1


def test_a_failure_marks_the_span_and_still_closes_the_trace() -> None:
    tracer = RecordingTracer()

    with pytest.raises(RuntimeError):
        with trace_operation("Outer", tracer=tracer):
            raise RuntimeError("boom")

    assert tracer.spans[0]["level"] == "ERROR"
    assert tracer.spans[0]["output"] == {"error": "boom"}
    assert tracer.flushes == 1
    assert isinstance(active_tracer(), NullTracer)


def test_without_a_tracer_nothing_is_recorded_and_nothing_breaks() -> None:
    with trace_operation("Outer") as operation:
        operation.output = {"done": True}
    assert isinstance(active_tracer(), NullTracer)


def test_a_model_call_inside_an_operation_becomes_a_generation_span() -> None:
    tracer = RecordingTracer()

    with trace_operation("Outer", tracer=tracer):
        call = traced_model_call(
            lambda payload: "Dear {{first_name}}",
            name="Model call: write client email",
            llm_client=FakeClient(),
            system="write an email",
        )
        text = call({"fund_name": "Money Market"})

    generation = tracer.spans[1]
    assert text == "Dear {{first_name}}"
    assert generation["name"] == "Model call: write client email"
    assert generation["as_type"] == "generation"
    assert generation["model"] == "claude-test"
    assert generation["input"] == {
        "system": "write an email",
        "payload": {"fund_name": "Money Market"},
    }
    assert generation["output"] == "Dear {{first_name}}"
    assert generation["parent"] is tracer.spans[0]


def test_a_model_call_outside_an_operation_is_left_alone() -> None:
    def model_call(payload):
        return "text"

    wrapped = traced_model_call(
        model_call, name="Model call", llm_client=FakeClient(), system="system"
    )

    assert wrapped is model_call


def test_a_failed_model_call_is_marked_on_its_span() -> None:
    tracer = RecordingTracer()

    def failing(payload):
        raise ValueError("model down")

    with trace_operation("Outer", tracer=tracer):
        call = traced_model_call(failing, name="Model call", llm_client=FakeClient(), system="s")
        with pytest.raises(ValueError):
            call({})

    assert tracer.spans[1]["level"] == "ERROR"


def test_trace_scope_names_the_trace_for_a_langfuse_tracer() -> None:
    from opentelemetry import context

    from app.llmops.tracing import LangfuseTracer, trace_scope

    tracer = LangfuseTracer(client=object())

    with trace_scope(tracer, name="Client email draft", tags=["email"], session_id="s-1"):
        with trace_scope(tracer, name="Campaign email draft", tags=["campaign"]):
            seen = {str(key): value for key, value in context.get_current().items()}

    assert seen["langfuse.propagated.trace_name"] == "Campaign email draft"
    assert seen["langfuse.propagated.session_id"] == "s-1"
    assert seen["langfuse.propagated.tags"] == ["email", "campaign"]


def test_trace_scope_does_nothing_for_the_null_tracer() -> None:
    from opentelemetry import context

    from app.llmops.tracing import trace_scope

    with trace_scope(NullTracer(), name="Anything"):
        seen = {str(key) for key in context.get_current()}

    assert "langfuse.propagated.trace_name" not in seen
