"""Reading back what happened during a run, in the order it happened.

A caller asks for everything after the place it last saw. While a run is
going it asks again every few seconds and gets the next few rows; once the
run is over the same call from the start returns the whole thing. Watching
live and opening a finished run are the same request with a different
starting point.

The short status that goes with the rows is counted from the rows
themselves, never from anywhere else, so a replay of a finished run shows
exactly what a person watching it live would have seen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models.agent_event import (
    APPROVAL_GIVEN,
    APPROVAL_NEEDED,
    INSIGHT_CREATED,
    INSIGHT_DISMISSED,
    INSIGHT_UPDATED,
    RUN_COMPLETED,
    RUN_STATUS,
    STEP_COMPLETED,
    STEP_STARTED,
    AgentEvent,
)
from app.db.models.agent_run import AgentRun

EVENT_PAGE_LIMIT = 500

GROUP_STARTED = "group_started"

GROUP_DONE = "group_done"

INVESTIGATING = "investigating"

STATE_CHANGE = "state"


@dataclass(frozen=True)
class RunProgress:
    """The short version of where a run has got to.

    Everything here is counted from the run's own events, so it is the same
    whether the run is still going or finished long ago.
    """

    groups_total: int | None = None
    groups_looked_at: int = 0
    insights_found: int = 0
    waiting_on_a_person: int = 0
    doing_now: str | None = None
    finished: bool = False
    outcome: str | None = None


@dataclass
class _Tally:
    """What the walk over the events is keeping track of."""

    groups_total: int | None = None
    groups_done: set[str] = field(default_factory=set)
    groups_started: set[str] = field(default_factory=set)
    open_steps: list[str] = field(default_factory=list)
    undecided_insights: set[int] = field(default_factory=set)
    insights_found: int = 0
    awaiting_approval: set[int] = field(default_factory=set)
    finished: bool = False
    outcome: str | None = None


def list_run_events(
    session: Session, run_id: int, *, after: int = 0, limit: int = EVENT_PAGE_LIMIT
) -> list[AgentEvent]:
    """The run's events after a given place, in order."""
    query = (
        select(AgentEvent)
        .where(AgentEvent.run_id == run_id, AgentEvent.ordinal > after)
        .order_by(AgentEvent.ordinal.asc())
        .limit(limit)
    )
    return list(session.scalars(query).all())


def last_ordinal(session: Session, run_id: int) -> int:
    """The place the run has reached, or zero if it has said nothing yet."""
    query = (
        select(AgentEvent.ordinal)
        .where(AgentEvent.run_id == run_id)
        .order_by(AgentEvent.ordinal.desc())
        .limit(1)
    )
    return int(session.scalar(query) or 0)


def run_progress(session: Session, run_id: int) -> RunProgress:
    """Where the run has got to, counted from every event it has written."""
    query = (
        select(AgentEvent.kind, AgentEvent.detail)
        .where(AgentEvent.run_id == run_id)
        .order_by(AgentEvent.ordinal.asc())
    )
    return progress_from(list(session.execute(query).all()))


def progress_from(rows: Sequence[tuple[str, dict]]) -> RunProgress:
    """The same count, over rows already in hand."""
    walk = ProgressWalk()
    for kind, detail in rows:
        walk.add(kind, detail)
    return walk.progress()


class ProgressWalk:
    """The same count, kept up to date as events arrive one at a time.

    A stream sees each event once and never wants to read the whole run again
    to say where it has got to, so it hands each one to this instead.
    """

    def __init__(self) -> None:
        self._tally = _Tally()

    def add(self, kind: str, detail: dict | None) -> None:
        _apply(self._tally, kind, detail or {})

    def progress(self) -> RunProgress:
        tally = self._tally
        return RunProgress(
            groups_total=tally.groups_total,
            groups_looked_at=len(tally.groups_done),
            insights_found=tally.insights_found,
            waiting_on_a_person=len(tally.undecided_insights) + len(tally.awaiting_approval),
            doing_now=None if tally.finished else _doing_now(tally),
            finished=tally.finished,
            outcome=tally.outcome,
        )


async def list_run_events_async(
    session: AsyncSession, run_id: int, *, after: int = 0, limit: int = EVENT_PAGE_LIMIT
) -> list[AgentEvent]:
    """The run's events after a given place, in order, without blocking."""
    query = (
        select(AgentEvent)
        .where(AgentEvent.run_id == run_id, AgentEvent.ordinal > after)
        .order_by(AgentEvent.ordinal.asc())
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


async def events_up_to_async(
    session: AsyncSession, run_id: int, ordinal: int
) -> list[tuple[str, dict]]:
    """Everything a resuming watcher already saw, so the count can be caught up."""
    if ordinal <= 0:
        return []
    query = (
        select(AgentEvent.kind, AgentEvent.detail)
        .where(AgentEvent.run_id == run_id, AgentEvent.ordinal <= ordinal)
        .order_by(AgentEvent.ordinal.asc())
    )
    return list((await session.execute(query)).all())


async def run_state_async(session: AsyncSession, run_id: int) -> str | None:
    """The run's state, or nothing at all if there is no such run."""
    query = select(AgentRun.state).where(AgentRun.run_id == run_id)
    return await session.scalar(query)


def _apply(tally: _Tally, kind: str, detail: dict) -> None:
    if kind == RUN_STATUS:
        _apply_status(tally, detail)
    elif kind == STEP_STARTED:
        tally.open_steps.append(str(detail.get("step", "")))
    elif kind == STEP_COMPLETED:
        _close_step(tally, str(detail.get("step", "")))
    elif kind == INSIGHT_CREATED:
        tally.insights_found += 1
        _add_id(tally.undecided_insights, detail.get("insight_id"))
    elif kind == INSIGHT_DISMISSED:
        _drop_id(tally.undecided_insights, detail.get("insight_id"))
    elif kind == INSIGHT_UPDATED and detail.get("change") == STATE_CHANGE:
        _drop_id(tally.undecided_insights, detail.get("insight_id"))
    elif kind == APPROVAL_NEEDED:
        _add_id(tally.awaiting_approval, detail.get("proposal_id"))
    elif kind == APPROVAL_GIVEN:
        _drop_id(tally.awaiting_approval, detail.get("proposal_id"))
    elif kind == RUN_COMPLETED:
        tally.finished = True
        tally.outcome = detail.get("state")
        tally.open_steps.clear()


def _apply_status(tally: _Tally, detail: dict) -> None:
    stage = detail.get("stage")
    if stage == INVESTIGATING:
        count = detail.get("group_count")
        tally.groups_total = int(count) if isinstance(count, int) else None
    elif stage == GROUP_STARTED:
        tally.groups_started.add(str(detail.get("group_name", "")))
    elif stage == GROUP_DONE:
        tally.groups_done.add(str(detail.get("group_name", "")))


def _close_step(tally: _Tally, step: str) -> None:
    if step in tally.open_steps:
        tally.open_steps.remove(step)


def _doing_now(tally: _Tally) -> str | None:
    working = tally.groups_started - tally.groups_done
    if working:
        return sorted(working)[0]
    return tally.open_steps[-1] if tally.open_steps else None


def _add_id(holder: set[int], value: object) -> None:
    if isinstance(value, int):
        holder.add(value)


def _drop_id(holder: set[int], value: object) -> None:
    if isinstance(value, int):
        holder.discard(value)
