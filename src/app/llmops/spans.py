"""Wrappers that put an agent's model calls and tool calls on the trace.

An agent step is one span, but the interesting part is inside it: how many
times it went to the model, what it sent, what came back, and how many
tokens each turn cost. These wrap the call so every turn becomes a
generation span carrying its own model name and token counts, which is
what lets the tracing tool work out the cost, and every tool call becomes
a span carrying what was asked and what came back.

Nothing here knows about a particular model client. A turn is anything
with text, tool requests and usage on it, so the blocking path and the
async path are wrapped the same way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

MODEL_CALL_SPAN = "model_call"


class ModelCallTally:
    """How many times one step went to the model, and what it spent.

    A conversation can take several turns, and a step can retry a whole
    conversation, so the number of model calls is not the number of steps.
    Counting the turns as they happen is the only honest way to price a run.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, turn: Any) -> None:
        self.calls += 1
        usage = getattr(turn, "usage", None)
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens

    def as_detail(self) -> dict[str, int]:
        return {
            "model_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def counting_converse(converse: Callable[..., Any], tally: ModelCallTally) -> Callable[..., Any]:
    """Wrap a blocking converse call so every turn it takes is counted."""

    def call(messages: list[dict[str, Any]]) -> Any:
        turn = converse(messages)
        tally.add(turn)
        return turn

    return call


def _turn_output(turn: Any) -> dict[str, Any]:
    """One turn, in the shape a person reads it on the trace."""
    return {
        "text": turn.text,
        "stop_reason": turn.stop_reason,
        "tool_calls": [
            {"tool_name": request.tool_name, "tool_input": dict(request.tool_input)}
            for request in turn.tool_requests
        ],
    }


def _usage_details(turn: Any) -> dict[str, int] | None:
    usage = getattr(turn, "usage", None)
    if usage is None:
        return None
    return {"input": usage.input_tokens, "output": usage.output_tokens}


def _call_input(system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"system": system, "messages": messages}


def traced_converse(
    converse: Callable[[list[dict[str, Any]]], Any],
    *,
    tracer: Any,
    trace_id: str,
    model: str,
    parent: Any = None,
    name: str = MODEL_CALL_SPAN,
    system: str,
    metadata: dict[str, Any] | None = None,
) -> Callable[[list[dict[str, Any]]], Any]:
    """Wrap a blocking converse call so every turn is a generation span."""

    def call(messages: list[dict[str, Any]]) -> Any:
        handle = tracer.start_span(
            trace_id=trace_id,
            name=name,
            input=_call_input(system, messages),
            metadata=metadata,
            as_type="generation",
            model=model,
            parent=parent,
        )
        try:
            turn = converse(messages)
        except Exception as exc:
            tracer.end_span(
                handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
            )
            raise
        tracer.end_span(handle, output=_turn_output(turn), usage_details=_usage_details(turn))
        return turn

    return call


def traced_aconverse(
    converse: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    tracer: Any,
    trace_id: str,
    model: str,
    parent: Any = None,
    name: str = MODEL_CALL_SPAN,
    system: str,
    metadata: dict[str, Any] | None = None,
) -> Callable[[list[dict[str, Any]]], Awaitable[Any]]:
    """The async twin of traced_converse, span for span."""

    async def call(messages: list[dict[str, Any]]) -> Any:
        handle = tracer.start_span(
            trace_id=trace_id,
            name=name,
            input=_call_input(system, messages),
            metadata=metadata,
            as_type="generation",
            model=model,
            parent=parent,
        )
        try:
            turn = await converse(messages)
        except Exception as exc:
            tracer.end_span(
                handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
            )
            raise
        tracer.end_span(handle, output=_turn_output(turn), usage_details=_usage_details(turn))
        return turn

    return call


def traced_tool_call(
    call_tool: Callable[[str, dict[str, Any]], Any],
    *,
    tracer: Any,
    trace_id: str,
    parent: Any = None,
) -> Callable[[str, dict[str, Any]], Any]:
    """Wrap a blocking tool executor so every call is a span of its own."""

    def call(tool_name: str, tool_input: dict[str, Any]) -> Any:
        handle = tracer.start_span(
            trace_id=trace_id, name=tool_name, input=tool_input, as_type="tool", parent=parent
        )
        try:
            output = call_tool(tool_name, tool_input)
        except Exception as exc:
            tracer.end_span(
                handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
            )
            raise
        tracer.end_span(handle, output=output, level=_tool_level(output))
        return output

    return call


def traced_async_tool_call(
    call_tool: Callable[[str, dict[str, Any]], Awaitable[Any]],
    *,
    tracer: Any,
    trace_id: str,
    parent: Any = None,
) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """The async twin of traced_tool_call."""

    async def call(tool_name: str, tool_input: dict[str, Any]) -> Any:
        handle = tracer.start_span(
            trace_id=trace_id, name=tool_name, input=tool_input, as_type="tool", parent=parent
        )
        try:
            output = await call_tool(tool_name, tool_input)
        except Exception as exc:
            tracer.end_span(
                handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
            )
            raise
        tracer.end_span(handle, output=output, level=_tool_level(output))
        return output

    return call


def _tool_level(output: Any) -> str | None:
    """A tool that refused is marked on the trace, so it stands out."""
    if isinstance(output, dict) and output.get("error"):
        return "WARNING"
    return None
