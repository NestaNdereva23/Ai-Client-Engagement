"""Runs one read tool, checks what it returns, and records the call."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.tools import TOOL_FUNCTIONS
from app.db.models.agent_run import AgentToolCall
from app.privacy.scanners import OutboundLeak, scan_outbound

logger = structlog.get_logger(__name__)

ToolCall = Callable[[str, dict[str, Any]], Any]


class UnknownTool(Exception):
    """The dispatcher was asked for a tool name that is not registered."""


def next_ordinal(session: Session, run_id: int) -> int:
    """The ordinal the next tool call in this run should use."""
    highest = session.scalar(
        select(func.coalesce(func.max(AgentToolCall.ordinal), 0)).where(
            AgentToolCall.run_id == run_id
        )
    )
    return highest + 1


def record_tool_call(
    session: Session,
    *,
    run_id: int,
    ordinal: int,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
) -> AgentToolCall:
    """Write one tool call to the trace. The caller owns the transaction."""
    row = AgentToolCall(
        run_id=run_id,
        ordinal=ordinal,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_output,
    )
    session.add(row)
    session.flush()
    return row


def run_tool(session: Session, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Call one read tool by name and return its output.

    Raises UnknownTool for a name outside the registry, and lets a TypeError
    through for an argument a tool does not accept: both mean the request
    was built wrong, not that the tool itself refused it. A tool's own
    refusals come back as an ordinary dict with an "error" key, the same
    shape as any other result.
    """
    function = TOOL_FUNCTIONS.get(tool_name)
    if function is None:
        raise UnknownTool(tool_name)
    return function(session, **tool_input)


def scanned_tool_output(output: dict[str, Any]) -> str:
    """The JSON text one tool result becomes, once it has passed the scanner.

    Raises OutboundLeak, unchanged, when that text carries a live contact
    channel. Nothing here strips the leak and carries on: a tool producing
    one is a bug in that tool, and the run must stop rather than send it
    on to the model.
    """
    rendered = json.dumps(output, sort_keys=True, default=str)
    scan_outbound(rendered)
    return rendered


def make_tool_executor(
    session: Session,
    run_id: int,
    *,
    extra_tools: Mapping[str, Callable[..., dict[str, Any]]] | None = None,
) -> ToolCall:
    """Build the call_tool function for one agent run.

    The result matches the shape app.privacy.boundary.run_conversation_boundary
    expects, so it can be handed to it directly. Every call this makes, a
    success, a refusal, an unknown tool name, or a bad argument, is written
    to agent_tool_call before it is returned; a call whose output is blocked
    by the scanner is written too, with the output withheld, and then raised.

    extra_tools adds tool functions beyond the read-only registry in
    app.agents.tools, for one step that needs a tool of its own, such as
    choose recording its decisions. Each takes the session first, then the
    call's arguments, exactly like a registered tool, and is dispatched,
    recorded and scanned the same way.
    """
    extra_tools = extra_tools or {}

    def call_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        ordinal = next_ordinal(session, run_id)
        logger.info(
            "agent_tool_call",
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_input=tool_input,
        )

        try:
            if tool_name in extra_tools:
                output = extra_tools[tool_name](session, **tool_input)
            else:
                output = run_tool(session, tool_name, tool_input)
        except UnknownTool:
            output = {
                "error": "unknown_tool",
                "message": f"'{tool_name}' is not a tool this agent can call",
            }
            logger.warning("agent_tool_call.unknown_tool", run_id=run_id, tool_name=tool_name)
            record_tool_call(
                session,
                run_id=run_id,
                ordinal=ordinal,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=output,
            )
            return output
        except TypeError as exc:
            output = {"error": "invalid_input", "message": str(exc)}
            logger.warning(
                "agent_tool_call.invalid_input", run_id=run_id, tool_name=tool_name, error=str(exc)
            )
            record_tool_call(
                session,
                run_id=run_id,
                ordinal=ordinal,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=output,
            )
            return output

        try:
            scanned_tool_output(output)
        except OutboundLeak:
            withheld = {"error": "output_blocked", "message": "this tool's answer was withheld"}
            logger.error("agent_tool_call.output_blocked", run_id=run_id, tool_name=tool_name)
            record_tool_call(
                session,
                run_id=run_id,
                ordinal=ordinal,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=withheld,
            )
            raise

        logger.info(
            "agent_tool_call.result",
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_output=output,
        )
        record_tool_call(
            session,
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=output,
        )
        return output

    return call_tool
