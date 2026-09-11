"""The async boundary is the blocking one, awaited.

These run one script of turns through both paths and prove they agree: the
same final answer, the same audit rows, and the same fail-closed stop on a
name in a turn of model text, in a tool argument, and in a tool result.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.privacy.boundary import (
    BoundaryAudit,
    ScannedConversation,
    run_conversation_boundary,
    run_conversation_boundary_async,
)
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest
from app.privacy.scanners import InboundLeak, OutboundLeak

ALLOWLISTED = {
    "recency_band": "Over 6y",
    "value_band": "High",
    "cadence_band": "Regular",
    "hold_band": "Unknown",
}

_USAGE = LLMUsage(input_tokens=0, output_tokens=0)

TurnScript = list[ConversationTurn]


def _turn(
    text: str = "", tool_requests: tuple[ToolUseRequest, ...] = (), stop_reason: str = "end_turn"
) -> ConversationTurn:
    return ConversationTurn(
        text=text, tool_requests=tool_requests, usage=_USAGE, stop_reason=stop_reason
    )


def _request(tool_input: dict[str, Any] | None = None) -> ToolUseRequest:
    return ToolUseRequest(
        call_id="call_1", tool_name="get_contact_history", tool_input=tool_input or {}
    )


def run_blocking(
    script: TurnScript,
    tool_output: Any,
    *,
    context: dict[str, Any] | None = None,
    identifiers: tuple[str, ...] = (),
    audit: list[BoundaryAudit] | None = None,
    seen_messages: list[list[dict[str, Any]]] | None = None,
) -> ScannedConversation:
    turns = iter(script)

    def converse(messages: list[dict[str, Any]]) -> ConversationTurn:
        if seen_messages is not None:
            seen_messages.append([dict(message) for message in messages])
        return next(turns)

    def call_tool(name: str, tool_input: dict[str, Any]) -> Any:
        return tool_output

    return run_conversation_boundary(
        ALLOWLISTED if context is None else context,
        converse,
        call_tool,
        identifiers=identifiers,
        max_turns=3,
        audit=None if audit is None else audit.append,
    )


async def run_async(
    script: TurnScript,
    tool_output: Any,
    *,
    context: dict[str, Any] | None = None,
    identifiers: tuple[str, ...] = (),
    audit: list[BoundaryAudit] | None = None,
    seen_messages: list[list[dict[str, Any]]] | None = None,
) -> ScannedConversation:
    turns = iter(script)

    async def converse(messages: list[dict[str, Any]]) -> ConversationTurn:
        if seen_messages is not None:
            seen_messages.append([dict(message) for message in messages])
        return next(turns)

    async def call_tool(name: str, tool_input: dict[str, Any]) -> Any:
        return tool_output

    return await run_conversation_boundary_async(
        ALLOWLISTED if context is None else context,
        converse,
        call_tool,
        identifiers=identifiers,
        max_turns=3,
        audit=None if audit is None else audit.append,
    )


def _tool_then_answer() -> TurnScript:
    return [
        _turn(
            tool_requests=(_request({"group_name": "quiet_high_value"}),), stop_reason="tool_use"
        ),
        _turn(text="Two groups matter tonight."),
    ]


async def test_both_paths_reach_the_same_answer_over_the_same_conversation() -> None:
    tool_output = {"sent_last_30_days": 2}
    blocking_audit: list[BoundaryAudit] = []
    async_audit: list[BoundaryAudit] = []
    blocking_messages: list[list[dict[str, Any]]] = []
    async_messages: list[list[dict[str, Any]]] = []

    blocking = run_blocking(
        _tool_then_answer(),
        tool_output,
        audit=blocking_audit,
        seen_messages=blocking_messages,
    )
    awaited = await run_async(
        _tool_then_answer(),
        tool_output,
        audit=async_audit,
        seen_messages=async_messages,
    )

    assert awaited == blocking
    assert awaited.final_text == "Two groups matter tonight."
    assert async_messages == blocking_messages
    assert async_audit == blocking_audit


async def test_both_paths_stop_at_the_turn_cap_the_same_way() -> None:
    script = [_turn(tool_requests=(_request(),), stop_reason="tool_use") for _ in range(3)]

    blocking = run_blocking(script, {"ok": True})
    awaited = await run_async(
        [_turn(tool_requests=(_request(),), stop_reason="tool_use") for _ in range(3)],
        {"ok": True},
    )

    assert awaited == blocking
    assert awaited.stopped_reason == "turn_limit"


async def test_both_paths_block_a_name_in_a_turn_of_model_text() -> None:
    script = [_turn(text="I would write to Jane Doe first.")]

    with pytest.raises(OutboundLeak):
        run_blocking(script, {}, identifiers=("Jane Doe",))
    with pytest.raises(OutboundLeak):
        await run_async(list(script), {}, identifiers=("Jane Doe",))


async def test_both_paths_block_a_name_in_a_tool_argument() -> None:
    def script() -> TurnScript:
        return [
            _turn(
                tool_requests=(_request({"client_name": "Jane Doe"}),),
                stop_reason="tool_use",
            ),
            _turn(text="done"),
        ]

    with pytest.raises(OutboundLeak):
        run_blocking(script(), {}, identifiers=("Jane Doe",))
    with pytest.raises(OutboundLeak):
        await run_async(script(), {}, identifiers=("Jane Doe",))


async def test_both_paths_block_a_name_in_a_tool_result() -> None:
    leaky = {"note": "Jane Doe called back yesterday"}

    with pytest.raises(OutboundLeak):
        run_blocking(_tool_then_answer(), leaky, identifiers=("Jane Doe",))
    with pytest.raises(OutboundLeak):
        await run_async(_tool_then_answer(), leaky, identifiers=("Jane Doe",))


async def test_both_paths_block_a_leaking_context_before_the_first_turn() -> None:
    leaky_context = {**ALLOWLISTED, "client_id": 1001}

    with pytest.raises(InboundLeak):
        run_blocking(_tool_then_answer(), {}, context=leaky_context)
    with pytest.raises(InboundLeak):
        await run_async(_tool_then_answer(), {}, context=leaky_context)


async def test_the_async_path_records_the_block_in_the_audit_too() -> None:
    records: list[BoundaryAudit] = []

    with pytest.raises(OutboundLeak):
        await run_async(
            _tool_then_answer(),
            {"note": "Jane Doe called back yesterday"},
            identifiers=("Jane Doe",),
            audit=records,
        )

    assert records[-1].outbound == "blocked"
    assert records[-1].fields == ["tool_output:get_contact_history"]
