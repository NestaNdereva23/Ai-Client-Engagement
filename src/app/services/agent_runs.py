"""Starting, listing, and reading back the agent's own runs."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.agents.agent_loop import execute_agent_run, start_agent_run
from app.config import Settings
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.session import SessionLocal
from app.llmops.tracing import get_shared_tracer
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor
from app.privacy.llm_client import get_agent_llm_client


class AgentRunNotFound(Exception):
    """No agent_run exists with the given id."""


def start_manual_run(session: Session, *, as_of: date | None = None) -> AgentRun:
    """Open a run for a person clicking the button. Raises AgentRunInProgress
    if one is already going, rather than queueing it silently.
    """
    return start_agent_run(session, trigger="manual", as_of=as_of)


def run_agent_in_background(run_id: int, *, as_of: date | None, settings: Settings) -> None:
    """Take a run start_manual_run already opened the rest of the way, in
    its own session. Meant to be handed to BackgroundTasks so the request
    that opened the run can answer immediately.
    """
    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        execute_agent_run(
            session,
            run,
            llm_client=get_agent_llm_client(settings),
            as_of=as_of,
            tracer=get_shared_tracer(),
        )


def get_agent_run(session: Session, run_id: int) -> AgentRun:
    """One run, or raise AgentRunNotFound."""
    run = session.get(AgentRun, run_id)
    if run is None:
        raise AgentRunNotFound(run_id)
    return run


def list_agent_runs(
    session: Session, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> tuple[list[AgentRun], str | None]:
    """One page of runs, newest first."""
    limit = clamp_limit(limit)
    rows = list(session.scalars(_agent_run_page_query(cursor, limit)).all())
    return _cut_page(rows, limit)


def _agent_run_page_query(cursor: str | None, limit: int) -> Select[tuple[AgentRun]]:
    """The one query behind both list_agent_runs and its async twin."""
    query = select(AgentRun)
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(AgentRun.run_id < after_id)
    return query.order_by(AgentRun.run_id.desc()).limit(limit + 1)


def _cut_page(rows: list[AgentRun], limit: int) -> tuple[list[AgentRun], str | None]:
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_id_cursor(rows[-1].run_id)
    return rows, None


async def get_agent_run_async(session: AsyncSession, run_id: int) -> AgentRun:
    """One run, or raise AgentRunNotFound. The async twin of get_agent_run."""
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise AgentRunNotFound(run_id)
    return run


async def list_agent_runs_async(
    session: AsyncSession, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> tuple[list[AgentRun], str | None]:
    """One page of runs, newest first. The async twin of list_agent_runs."""
    limit = clamp_limit(limit)
    result = await session.scalars(_agent_run_page_query(cursor, limit))
    return _cut_page(list(result.all()), limit)


async def list_run_tool_calls_async(session: AsyncSession, run_id: int) -> list[AgentToolCall]:
    """Every tool call a run has recorded so far, in the order it made them."""
    query = (
        select(AgentToolCall)
        .where(AgentToolCall.run_id == run_id)
        .order_by(AgentToolCall.ordinal.asc())
    )
    result = await session.scalars(query)
    return list(result.all())
