"""The single code path to the model API.

Every model call goes through run_model_boundary or run_conversation_boundary:
each one takes only allow-listed, bucketed context, scans it, calls the model,
then scans what comes back. A tool calling conversation is scanned the same
way on every turn it takes, tool results included, since a tool result is one
more thing arriving from outside the model and must never carry a name, a
code, an exact amount, or a date any more than a drafted reply may. No other
module talks to the model client directly. Re-attaching real values happens
afterwards, outside this function.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.privacy.scanners import (
    MODEL_ALLOWED_KEYS,
    InboundLeak,
    OutboundLeak,
    scan_inbound,
    scan_outbound,
)

# A model call takes an allow-listed payload and returns the drafted text.
ModelCall = Callable[[dict[str, Any]], str]


class ToolRequestLike(Protocol):
    """The part of a tool request the boundary needs: its name and its input."""

    call_id: str
    tool_name: str
    tool_input: Mapping[str, Any]


class ConversationTurnLike(Protocol):
    """The part of a model turn the boundary needs: its text and any tool requests."""

    text: str
    tool_requests: Sequence[ToolRequestLike]


ConverseCall = Callable[[list[dict[str, Any]]], ConversationTurnLike]

ToolCall = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True)
class ScannedConversation:
    """How a privacy scanned conversation ended."""

    final_text: str | None
    stopped_reason: Literal["final_answer", "turn_limit"]


@dataclass
class BoundaryAudit:
    """One model crossing: the fields sent, the scanner verdicts, and its refs."""

    fields: list[str]
    inbound: str = "skipped"  # pass | blocked | skipped
    outbound: str = "skipped"
    entity_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    reason: str | None = None


# An audit sink receives one record per crossing, whether it passed or blocked.
AuditSink = Callable[[BoundaryAudit], None]


def to_model_context(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a safe row to the allow-list, dropping client_id and anything else."""
    return {key: row[key] for key in MODEL_ALLOWED_KEYS if key in row}


def render_model_context(payload: Mapping[str, Any]) -> str:
    """Turn an allow listed payload into the plain text a model call sends."""
    return "\n".join(f"{key}: {value}" for key, value in sorted(payload.items()))


def run_model_boundary(
    context: Mapping[str, Any],
    model_call: ModelCall,
    *,
    identifiers: Iterable[str] = (),
    entity_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    audit: AuditSink | None = None,
) -> str:
    """Send allow listed context to the model and return the drafted text.

    identifiers are the request's real client values; the scanners block them
    from the payload and from the draft. Fails closed: an inbound hit aborts
    before the call, an outbound hit aborts after it, returning nothing. The
    audit sink, when given, records one row per crossing including blocks.
    """
    payload = dict(context)
    identifiers = tuple(identifiers)
    record = BoundaryAudit(
        fields=sorted(payload), entity_id=entity_id, run_id=run_id, trace_id=trace_id
    )
    try:
        scan_inbound(payload, identifiers)
        record.inbound = "pass"
        draft = model_call(payload)
        scan_outbound(draft, identifiers)
        record.outbound = "pass"
        return draft
    except InboundLeak as leak:
        record.inbound = "blocked"
        record.reason = str(leak)
        raise
    except OutboundLeak as leak:
        record.outbound = "blocked"
        record.reason = str(leak)
        raise
    finally:
        if audit is not None:
            audit(record)


def assistant_content_blocks(turn: ConversationTurnLike) -> list[dict[str, Any]]:
    """The assistant message blocks one turn becomes, once it joins the history."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": turn.text}] if turn.text else []
    blocks.extend(
        {
            "type": "tool_use",
            "id": request.call_id,
            "name": request.tool_name,
            "input": dict(request.tool_input),
        }
        for request in turn.tool_requests
    )
    return blocks


def tool_result_block(call_id: str, output: Any) -> dict[str, Any]:
    """One tool result rendered the way the model expects it back."""
    content = output if isinstance(output, str) else json.dumps(output)
    return {"type": "tool_result", "tool_use_id": call_id, "content": content}


def run_conversation_boundary(
    context: Mapping[str, Any],
    converse: ConverseCall,
    call_tool: ToolCall,
    *,
    identifiers: Iterable[str] = (),
    max_turns: int,
    entity_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    audit: AuditSink | None = None,
) -> ScannedConversation:
    """Hold a tool calling conversation, scanning every turn on the way out and back.

    The context is scanned once, before the first turn, the same way
    run_model_boundary scans a single shot payload. After that, every turn's
    text and every tool call's input are scanned as they arrive from the
    model, and every tool result is scanned before it is added back to the
    history and sent to the model again. Fails closed: a hit stops the run
    right there, never quietly stripped so the run can carry on. The audit
    sink, when given, records one row per crossing, including the one that
    failed.
    """
    identifiers = tuple(identifiers)
    payload = dict(context)

    def record(
        fields: list[str],
        *,
        inbound: str = "skipped",
        outbound: str = "skipped",
        reason: str | None = None,
    ) -> None:
        if audit is not None:
            audit(
                BoundaryAudit(
                    fields=fields,
                    inbound=inbound,
                    outbound=outbound,
                    entity_id=entity_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    reason=reason,
                )
            )

    def scan_turn(turn: ConversationTurnLike) -> None:
        try:
            scan_outbound(turn.text, identifiers)
        except OutboundLeak as leak:
            record(["turn_text"], outbound="blocked", reason=str(leak))
            raise
        record(["turn_text"], outbound="pass")

        for request in turn.tool_requests:
            rendered_input = json.dumps(dict(request.tool_input), sort_keys=True)
            try:
                scan_outbound(rendered_input, identifiers)
            except OutboundLeak as leak:
                record([f"tool_input:{request.tool_name}"], outbound="blocked", reason=str(leak))
                raise
            record([f"tool_input:{request.tool_name}"], outbound="pass")

    def run_tools(turn: ConversationTurnLike) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for request in turn.tool_requests:
            output = call_tool(request.tool_name, dict(request.tool_input))
            block = tool_result_block(request.call_id, output)
            try:
                scan_outbound(block["content"], identifiers)
            except OutboundLeak as leak:
                record([f"tool_output:{request.tool_name}"], outbound="blocked", reason=str(leak))
                raise
            record([f"tool_output:{request.tool_name}"], outbound="pass")
            blocks.append(block)
        return blocks

    try:
        scan_inbound(payload, identifiers)
    except InboundLeak as leak:
        record(sorted(payload), inbound="blocked", reason=str(leak))
        raise
    record(sorted(payload), inbound="pass")

    starter_text = render_model_context(payload) or "Begin."
    messages: list[dict[str, Any]] = [{"role": "user", "content": starter_text}]

    for turn_number in range(1, max_turns + 1):
        turn = converse(messages)
        scan_turn(turn)

        if not turn.tool_requests:
            return ScannedConversation(final_text=turn.text, stopped_reason="final_answer")
        if turn_number == max_turns:
            break

        messages.append({"role": "assistant", "content": assistant_content_blocks(turn)})
        messages.append({"role": "user", "content": run_tools(turn)})

    return ScannedConversation(final_text=None, stopped_reason="turn_limit")


def prepare_batch_payload(
    context: Mapping[str, Any],
    *,
    identifiers: Iterable[str] = (),
    entity_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    audit: AuditSink | None = None,
) -> dict[str, Any]:
    """Scan one client's payload before it is bundled into a batch request.

    The batch path splits run_model_boundary's single call in two, since the
    model's reply does not come back until the batch is ingested, possibly a
    day later: this is the inbound half, run at submit time. Same scanner,
    same fail-closed behaviour, same BoundaryAudit shape, so a batch crossing
    shows up in audit_log exactly like a synchronous one; scan_batch_result
    is the matching outbound half, run once the reply is available.
    """
    payload = dict(context)
    identifiers = tuple(identifiers)
    record = BoundaryAudit(
        fields=sorted(payload), entity_id=entity_id, run_id=run_id, trace_id=trace_id
    )
    try:
        scan_inbound(payload, identifiers)
        record.inbound = "pass"
        return payload
    except InboundLeak as leak:
        record.inbound = "blocked"
        record.reason = str(leak)
        raise
    finally:
        if audit is not None:
            audit(record)


def scan_batch_result(
    draft: str,
    *,
    identifiers: Iterable[str] = (),
    entity_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    audit: AuditSink | None = None,
) -> str:
    """Scan one batch result before anything downstream reads it.

    The outbound half of prepare_batch_payload: fails closed exactly like
    run_model_boundary's own outbound scan, raising OutboundLeak and
    returning nothing rather than letting a leaked draft through.
    """
    identifiers = tuple(identifiers)
    record = BoundaryAudit(
        fields=[], entity_id=entity_id, run_id=run_id, trace_id=trace_id, inbound="skipped"
    )
    try:
        scan_outbound(draft, identifiers)
        record.outbound = "pass"
        return draft
    except OutboundLeak as leak:
        record.outbound = "blocked"
        record.reason = str(leak)
        raise
    finally:
        if audit is not None:
            audit(record)
