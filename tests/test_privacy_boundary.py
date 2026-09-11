"""The privacy boundary is the single, fail-closed path to the model.

These prove the payload is projected to the allow-list, both scanners run around
the call, a hit aborts without returning a draft, and no module outside
app.privacy imports the model SDK directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.privacy.boundary as boundary
from app.privacy.boundary import (
    render_model_context,
    run_conversation_boundary,
    run_model_boundary,
    to_model_context,
)
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest
from app.privacy.scanners import InboundLeak, OutboundLeak, scan_inbound

ALLOWLISTED = {
    "recency_band": "Over 6y",
    "value_band": "High",
    "cadence_band": "Regular",
    "hold_band": "Unknown",
}

_USAGE = LLMUsage(input_tokens=0, output_tokens=0)


def _turn(
    text: str = "", tool_requests: tuple[ToolUseRequest, ...] = (), stop_reason: str = "end_turn"
) -> ConversationTurn:
    return ConversationTurn(
        text=text, tool_requests=tool_requests, usage=_USAGE, stop_reason=stop_reason
    )


def test_to_model_context_keeps_only_allowlisted_keys() -> None:
    row = {**ALLOWLISTED, "client_id": 1001, "own_rhythm_days": 42, "client_name": "Jane"}
    assert to_model_context(row) == ALLOWLISTED


def test_run_model_boundary_sends_only_the_allowlisted_payload() -> None:
    seen: dict = {}

    def model_call(payload: dict) -> str:
        seen.update(payload)
        return "Hi {{first_name}}"

    draft = run_model_boundary(ALLOWLISTED, model_call)
    assert draft == "Hi {{first_name}}"
    assert seen == ALLOWLISTED


def test_inbound_hit_aborts_before_the_model_is_called() -> None:
    called = False

    def model_call(payload: dict) -> str:
        nonlocal called
        called = True
        return "draft"

    # client_id is a re-attachment key, not allow-listed, so it must never be sent.
    with pytest.raises(InboundLeak):
        run_model_boundary({**ALLOWLISTED, "client_id": 1001}, model_call)
    assert called is False


def test_boundary_blocks_a_draft_that_echoes_a_real_identifier() -> None:
    with pytest.raises(OutboundLeak):
        run_model_boundary(
            ALLOWLISTED,
            lambda payload: "Warm regards to Jane Doe",
            identifiers=["Jane Doe"],
        )


def test_boundary_returns_a_clean_placeholder_draft_with_identifiers_set() -> None:
    draft = run_model_boundary(
        ALLOWLISTED,
        lambda payload: "Dear {{first_name}}, come back soon.",
        identifiers=["Jane Doe"],
    )
    assert draft == "Dear {{first_name}}, come back soon."


def test_outbound_hit_aborts_after_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocking_scan(draft: str, identifiers=()) -> None:
        raise OutboundLeak("echoed an identifier")

    monkeypatch.setattr(boundary, "scan_outbound", blocking_scan)
    with pytest.raises(OutboundLeak):
        run_model_boundary(ALLOWLISTED, lambda payload: "leaky draft")


def test_run_conversation_boundary_sends_the_scanned_context_as_the_first_message() -> None:
    seen_messages: list[list[dict]] = []

    def converse(messages: list[dict]) -> ConversationTurn:
        seen_messages.append(messages)
        return _turn("no action needed tonight")

    result = run_conversation_boundary(ALLOWLISTED, converse, lambda name, args: {}, max_turns=3)

    assert result.final_text == "no action needed tonight"
    assert result.stopped_reason == "final_answer"
    assert seen_messages[0] == [{"role": "user", "content": render_model_context(ALLOWLISTED)}]


def test_run_conversation_boundary_blocks_before_the_first_turn_when_the_context_leaks() -> None:
    called = False

    def converse(messages: list[dict]) -> ConversationTurn:
        nonlocal called
        called = True
        return _turn("draft")

    with pytest.raises(InboundLeak):
        run_conversation_boundary(
            {**ALLOWLISTED, "client_id": 1001}, converse, lambda name, args: {}, max_turns=3
        )
    assert called is False


def test_run_conversation_boundary_runs_a_tool_and_returns_the_final_answer() -> None:
    first_turn = _turn(
        tool_requests=(ToolUseRequest(call_id="call_1", tool_name="list_groups", tool_input={}),),
        stop_reason="tool_use",
    )
    second_turn = _turn("three groups need attention tonight")
    turns = iter([first_turn, second_turn])
    seen_tools: list[tuple[str, dict]] = []

    def converse(messages: list[dict]) -> ConversationTurn:
        return next(turns)

    def call_tool(name: str, tool_input: dict) -> dict:
        seen_tools.append((name, tool_input))
        return {"groups": 3}

    result = run_conversation_boundary(ALLOWLISTED, converse, call_tool, max_turns=3)

    assert seen_tools == [("list_groups", {})]
    assert result.final_text == "three groups need attention tonight"
    assert result.stopped_reason == "final_answer"


def test_run_conversation_boundary_blocks_a_name_in_a_model_turn() -> None:
    tool_called = False

    def converse(messages: list[dict]) -> ConversationTurn:
        return _turn(
            "checking on Jane Doe now",
            tool_requests=(
                ToolUseRequest(call_id="call_1", tool_name="list_groups", tool_input={}),
            ),
            stop_reason="tool_use",
        )

    def call_tool(name: str, tool_input: dict) -> dict:
        nonlocal tool_called
        tool_called = True
        return {}

    with pytest.raises(OutboundLeak):
        run_conversation_boundary(
            ALLOWLISTED, converse, call_tool, identifiers=["Jane Doe"], max_turns=3
        )
    assert tool_called is False


def test_run_conversation_boundary_blocks_a_name_in_a_tool_argument() -> None:
    tool_called = False

    def converse(messages: list[dict]) -> ConversationTurn:
        return _turn(
            tool_requests=(
                ToolUseRequest(
                    call_id="call_1",
                    tool_name="flag_for_account_manager",
                    tool_input={"note": "for Jane Doe"},
                ),
            ),
            stop_reason="tool_use",
        )

    def call_tool(name: str, tool_input: dict) -> dict:
        nonlocal tool_called
        tool_called = True
        return {}

    with pytest.raises(OutboundLeak):
        run_conversation_boundary(
            ALLOWLISTED, converse, call_tool, identifiers=["Jane Doe"], max_turns=3
        )
    assert tool_called is False


def test_run_conversation_boundary_blocks_a_name_in_a_tool_result() -> None:
    second_turn_requested = False

    def converse(messages: list[dict]) -> ConversationTurn:
        nonlocal second_turn_requested
        if len(messages) > 1:
            second_turn_requested = True
        return _turn(
            tool_requests=(
                ToolUseRequest(call_id="call_1", tool_name="get_contact_history", tool_input={}),
            ),
            stop_reason="tool_use",
        )

    def call_tool(name: str, tool_input: dict) -> dict:
        return {"note": "Jane Doe called back yesterday"}

    with pytest.raises(OutboundLeak):
        run_conversation_boundary(
            ALLOWLISTED, converse, call_tool, identifiers=["Jane Doe"], max_turns=3
        )
    assert second_turn_requested is False


def test_run_conversation_boundary_stops_at_the_turn_cap() -> None:
    call_count = 0

    def converse(messages: list[dict]) -> ConversationTurn:
        nonlocal call_count
        call_count += 1
        return _turn(
            tool_requests=(
                ToolUseRequest(
                    call_id=f"call_{call_count}", tool_name="list_groups", tool_input={}
                ),
            ),
            stop_reason="tool_use",
        )

    tool_calls: list[str] = []

    def call_tool(name: str, tool_input: dict) -> dict:
        tool_calls.append(name)
        return {}

    result = run_conversation_boundary(ALLOWLISTED, converse, call_tool, max_turns=3)

    assert result.stopped_reason == "turn_limit"
    assert result.final_text is None
    assert call_count == 3
    assert len(tool_calls) == 2


def test_run_conversation_boundary_audits_every_crossing_including_the_block() -> None:
    records: list[boundary.BoundaryAudit] = []

    def converse(messages: list[dict]) -> ConversationTurn:
        return _turn("Warm regards to Jane Doe")

    with pytest.raises(OutboundLeak):
        run_conversation_boundary(
            ALLOWLISTED,
            converse,
            lambda name, tool_input: {},
            identifiers=["Jane Doe"],
            max_turns=3,
            audit=records.append,
        )

    assert len(records) == 2
    assert records[0].inbound == "pass"
    assert records[1].outbound == "blocked"
    assert records[1].reason is not None


def test_scan_inbound_rejects_offlist_keys() -> None:
    with pytest.raises(InboundLeak, match="fact-block"):
        scan_inbound({"client_name": "Jane"})


def test_scan_inbound_passes_allowlisted_only() -> None:
    assert scan_inbound(ALLOWLISTED) is None


def test_only_privacy_imports_the_model_sdk() -> None:
    """No module outside app.privacy may import the model SDK directly."""
    app_root = Path(boundary.__file__).parents[1]
    privacy_dir = app_root / "privacy"
    sdk_import = re.compile(r"^\s*(import|from)\s+anthropic\b", re.MULTILINE)

    offenders = [
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if privacy_dir not in path.parents and sdk_import.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
