"""Runs one read tool, checks what it returns, and records the call.

Every tool in app.agents.tools returns a plain dict, whether it succeeds or
refuses. This module is the one place a tool name turns into an actual call:
it renders the result the same way a tool result is rendered for the model,
runs it past the same privacy scanner every other model crossing goes
through, and only then writes the call to agent_tool_call so it can be read
back later. A tool whose output somehow carried a live contact channel is
never handed back or stored as written: the call is recorded with the reason
it was withheld, and the block is raised so the run stops rather than carry
a leak forward in its own history.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.tools import TOOL_FUNCTIONS
from app.db.models.agent_run import AgentToolCall
from app.privacy.scanners import OutboundLeak, scan_outbound

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


def make_tool_executor(session: Session, run_id: int) -> ToolCall:
    """Build the call_tool function for one agent run.

    The result matches the shape app.privacy.boundary.run_conversation_boundary
    expects, so it can be handed to it directly. Every call this makes, a
    success, a refusal, an unknown tool name, or a bad argument, is written
    to agent_tool_call before it is returned; a call whose output is blocked
    by the scanner is written too, with the output withheld, and then raised.
    """

    def call_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        ordinal = next_ordinal(session, run_id)

        try:
            output = run_tool(session, tool_name, tool_input)
        except UnknownTool:
            output = {
                "error": "unknown_tool",
                "message": f"'{tool_name}' is not a tool this agent can call",
            }
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
            record_tool_call(
                session,
                run_id=run_id,
                ordinal=ordinal,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=withheld,
            )
            raise

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
