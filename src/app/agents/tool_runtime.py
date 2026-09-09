"""Runs one read tool, checks what it returns, and records the call."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.agents.events import NO_EVENTS, EventLog, tool_finished
from app.agents.query_tools import QUERY_TOOL_FUNCTIONS, QUERY_TOOL_NAMES
from app.agents.tools import TOOL_FUNCTIONS
from app.config import get_settings
from app.db.models.agent_event import TOOL_STARTED
from app.db.models.agent_run import AgentToolCall
from app.privacy.scanners import OutboundLeak, scan_outbound

logger = structlog.get_logger(__name__)

ToolCall = Callable[[str, dict[str, Any]], Any]

AsyncToolCall = Callable[[str, dict[str, Any]], Awaitable[Any]]


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


class CallBudget:
    """How many investigation queries one caller may still spend.

    Counting the run's own rows back from the database is enough for a
    single conversation. A run investigating several groups at the same
    time needs a budget per group instead, or the first group to get going
    spends what the others were going to use.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._taken = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        """Claim one query, or answer False when they are all spent."""
        with self._lock:
            if self._taken >= self.limit:
                return False
            self._taken += 1
            return True


class OrdinalSource:
    """Hands out the next tool call ordinal for one run, one caller at a time.

    Reading the highest ordinal back from the database works while a run
    makes one call after another. A run investigating several groups at the
    same time would read the same highest ordinal twice and collide on the
    unique key, so the counter is kept here instead and guarded by a lock.
    """

    def __init__(self, start: int) -> None:
        self._next = start
        self._lock = threading.Lock()

    @classmethod
    def from_database(cls, session: Session, run_id: int) -> OrdinalSource:
        """A counter carrying on from whatever this run has already recorded."""
        return cls(next_ordinal(session, run_id))

    def take(self) -> int:
        with self._lock:
            ordinal = self._next
            self._next += 1
        return ordinal


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
    ordinals: OrdinalSource | None = None,
    events: EventLog = NO_EVENTS,
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
        ordinal = ordinals.take() if ordinals is not None else next_ordinal(session, run_id)
        logger.info(
            "agent_tool_call",
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        events.record(TOOL_STARTED, tool_name=tool_name, ordinal=ordinal)

        def done(output: dict[str, Any]) -> dict[str, Any]:
            record_tool_call(
                session,
                run_id=run_id,
                ordinal=ordinal,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=output,
            )
            tool_finished(events, tool_name=tool_name, ordinal=ordinal, output=output)
            return output

        try:
            if tool_name in extra_tools:
                output = extra_tools[tool_name](session, **tool_input)
            else:
                output = run_tool(session, tool_name, tool_input)
        except UnknownTool:
            logger.warning("agent_tool_call.unknown_tool", run_id=run_id, tool_name=tool_name)
            return done(
                {
                    "error": "unknown_tool",
                    "message": f"'{tool_name}' is not a tool this agent can call",
                }
            )
        except TypeError as exc:
            logger.warning(
                "agent_tool_call.invalid_input", run_id=run_id, tool_name=tool_name, error=str(exc)
            )
            return done({"error": "invalid_input", "message": str(exc)})

        try:
            scanned_tool_output(output)
        except OutboundLeak:
            logger.error("agent_tool_call.output_blocked", run_id=run_id, tool_name=tool_name)
            done({"error": "output_blocked", "message": "this tool's answer was withheld"})
            raise

        logger.info(
            "agent_tool_call.result",
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_output=output,
        )
        return done(output)

    return call_tool


def make_async_tool_executor(
    session: Session,
    run_id: int,
    *,
    extra_tools: Mapping[str, Callable[..., dict[str, Any]]] | None = None,
    ordinals: OrdinalSource | None = None,
    events: EventLog = NO_EVENTS,
) -> AsyncToolCall:
    """The async twin of make_tool_executor, for a run that awaits its tools.

    Every tool here reads the database through the blocking session, so the
    work runs in a worker thread rather than on the event loop. It is the
    same executor underneath: the dispatch, the scanner and the tool call
    record are one code path, not two.
    """
    call_tool = make_tool_executor(
        session, run_id, extra_tools=extra_tools, ordinals=ordinals, events=events
    )

    async def async_call_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(call_tool, tool_name, tool_input)

    return async_call_tool


async def next_ordinal_async(session: AsyncSession, run_id: int) -> int:
    """The async twin of next_ordinal."""
    highest = await session.scalar(
        select(func.coalesce(func.max(AgentToolCall.ordinal), 0)).where(
            AgentToolCall.run_id == run_id
        )
    )
    return highest + 1


async def record_tool_call_async(
    session: AsyncSession,
    *,
    run_id: int,
    ordinal: int,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
) -> None:
    """The async twin of record_tool_call.

    This one commits. An investigation query that runs too long rolls its
    own transaction back, and the trace of what was asked must survive that.
    """
    session.add(
        AgentToolCall(
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
        )
    )
    await session.commit()


def make_async_query_executor(
    session: AsyncSession,
    run_id: int,
    *,
    tools: Mapping[str, Callable[..., Awaitable[dict[str, Any]]]] | None = None,
    ordinals: OrdinalSource | None = None,
    budget: CallBudget | None = None,
    events: EventLog = NO_EVENTS,
) -> AsyncToolCall:
    """Build the call_tool function for the agent's own investigation queries.

    These tools read through the async session rather than a worker thread,
    because waiting on the database is most of what they do. Everything else
    matches the blocking executor: an unknown name, a bad argument and a
    tool's own refusal all come back as an ordinary dict, every call is
    written to agent_tool_call, and no output reaches the model before the
    scanner has seen it. One extra rule lives here: a caller may only spend so
    many of these, and the call past the budget is refused and recorded.
    """
    registry = dict(tools or QUERY_TOOL_FUNCTIONS)
    budget = budget or CallBudget(get_settings().agent_query_call_budget)

    async def call_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        ordinal = (
            ordinals.take() if ordinals is not None else await next_ordinal_async(session, run_id)
        )
        logger.info(
            "agent_query_call",
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        events.record(TOOL_STARTED, tool_name=tool_name, ordinal=ordinal)

        async def finish(output: dict[str, Any]) -> dict[str, Any]:
            await record_tool_call_async(
                session,
                run_id=run_id,
                ordinal=ordinal,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=output,
            )
            tool_finished(events, tool_name=tool_name, ordinal=ordinal, output=output)
            return output

        function = registry.get(tool_name)
        if function is None:
            return await finish(
                {
                    "error": "unknown_tool",
                    "message": f"'{tool_name}' is not a tool this agent can call",
                }
            )

        if not budget.take():
            logger.warning("agent_query_call.budget_spent", run_id=run_id, budget=budget.limit)
            return await finish(
                {
                    "error": "budget_spent",
                    "message": (f"this investigation has used all {budget.limit} of its queries"),
                }
            )

        try:
            output = await function(session, **tool_input)
        except TypeError as exc:
            return await finish({"error": "invalid_input", "message": str(exc)})

        try:
            scanned_tool_output(output)
        except OutboundLeak:
            logger.error("agent_query_call.output_blocked", run_id=run_id, tool_name=tool_name)
            await finish({"error": "output_blocked", "message": "this tool's answer was withheld"})
            raise

        logger.info(
            "agent_query_call.result",
            run_id=run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            tool_output=output,
        )
        return await finish(output)

    return call_tool


def make_investigation_tool_executor(
    *,
    blocking_session: Session,
    query_session: AsyncSession,
    run_id: int,
    write_tools: Mapping[str, Callable[..., dict[str, Any]]],
    ordinals: OrdinalSource,
    query_budget: CallBudget,
    events: EventLog = NO_EVENTS,
) -> AsyncToolCall:
    """One call_tool for an investigation, over both kinds of tool it has.

    The investigation queries read through the async session, because
    waiting on the database is most of what they do, and they come out of
    this investigation's own query budget rather than a shared one. The read tools and the
    tools that write a finding down go through the blocking session in a
    worker thread. Both halves are the executors above, unchanged, so the
    dispatch, the scanner and the tool call record stay one code path.
    """
    query_call = make_async_query_executor(
        query_session, run_id, ordinals=ordinals, budget=query_budget, events=events
    )
    other_call = make_async_tool_executor(
        blocking_session, run_id, extra_tools=write_tools, ordinals=ordinals, events=events
    )

    async def call_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name in QUERY_TOOL_NAMES:
            return await query_call(tool_name, tool_input)
        return await other_call(tool_name, tool_input)

    return call_tool
