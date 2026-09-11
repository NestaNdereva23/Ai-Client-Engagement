"""Pushing a run's events to a watcher instead of making it ask.

The rows are the same ones the polling endpoint reads. What changes is who
speaks first: the request stays open and each event is sent as it is written,
so the panel fills in as the run happens.

Nothing here holds a worker thread. The whole path is async, on the async
session, so a stream that sits quiet for an hour costs the API almost
nothing while it waits.

Every message carries the place it sits in the run, so a watcher that loses
its connection can say where it got to and carry on from there without
repeating or skipping anything.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass

import anyio

from app.config import Settings
from app.db.async_session import AsyncSessionLocal
from app.services.agent_events import (
    EVENT_PAGE_LIMIT,
    ProgressWalk,
    events_up_to_async,
    list_run_events_async,
    run_state_async,
)

EVENT_MESSAGE = "activity"

PROGRESS_MESSAGE = "progress"

END_MESSAGE = "end"

RUNNING = "running"

STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@dataclass
class StreamSlots:
    """How many watchers may hold a stream open at once.

    Each open stream keeps a database connection, so this is what stops a
    handful of forgotten browser tabs starving the rest of the API.
    """

    limit: int
    open_now: int = 0

    def take(self) -> bool:
        if self.open_now >= self.limit:
            return False
        self.open_now += 1
        return True

    def give_back(self) -> None:
        self.open_now = max(0, self.open_now - 1)


def frame(name: str, payload: dict, ordinal: int | None = None) -> str:
    """One server sent message, with its place in the run as the id."""
    lines = []
    if ordinal is not None:
        lines.append(f"id: {ordinal}")
    lines.append(f"event: {name}")
    lines.append(f"data: {json.dumps(payload, default=str)}")
    return "\n".join(lines) + "\n\n"


def keepalive() -> str:
    """A comment the browser ignores, sent so the proxy sees the line is alive."""
    return ": still here\n\n"


def event_payload(event) -> dict:
    return {
        "ordinal": event.ordinal,
        "kind": event.kind,
        "detail": event.detail or {},
        "created_at": event.created_at,
    }


def resume_point(after: int, last_event_id: str | None) -> int:
    """Where to carry on from.

    A browser reconnecting on its own sends the last id it saw as a header
    rather than in the query, so that wins when both are there.
    """
    if last_event_id:
        try:
            return max(0, int(last_event_id))
        except ValueError:
            return after
    return after


async def stream_run_events(
    run_id: int,
    *,
    after: int,
    settings: Settings,
    slots: StreamSlots,
) -> AsyncIterator[str]:
    """Every event from a point onwards, then the end of the run.

    The slot is given back however the stream ends, including when the
    watcher closes the tab and this is cancelled part way through.
    """
    session = AsyncSessionLocal()
    try:
        async for message in _pump(session, run_id, after, settings):
            yield message
    finally:
        await session.close()
        slots.give_back()


async def _pump(session, run_id: int, after: int, settings: Settings) -> AsyncIterator[str]:
    walk = ProgressWalk()
    for kind, detail in await events_up_to_async(session, run_id, after):
        walk.add(kind, detail)

    cursor = after
    quiet_for = 0.0
    waited = 0.0

    yield frame(PROGRESS_MESSAGE, asdict(walk.progress()), ordinal=cursor)

    while True:
        events = await list_run_events_async(session, run_id, after=cursor, limit=EVENT_PAGE_LIMIT)
        for event in events:
            walk.add(event.kind, event.detail)
            cursor = event.ordinal
            yield frame(EVENT_MESSAGE, event_payload(event), ordinal=event.ordinal)

        if events:
            quiet_for = 0.0
            yield frame(PROGRESS_MESSAGE, asdict(walk.progress()), ordinal=cursor)
            if len(events) == EVENT_PAGE_LIMIT:
                continue

        state = await run_state_async(session, run_id)
        if state != RUNNING:
            yield frame(END_MESSAGE, {"state": state, "last_ordinal": cursor}, ordinal=cursor)
            return

        if waited >= settings.agent_stream_max_seconds:
            yield frame(END_MESSAGE, {"state": state, "last_ordinal": cursor}, ordinal=cursor)
            return

        if quiet_for >= settings.agent_stream_keepalive_seconds:
            quiet_for = 0.0
            yield keepalive()

        await session.rollback()
        await anyio.sleep(settings.agent_stream_poll_seconds)
        quiet_for += settings.agent_stream_poll_seconds
        waited += settings.agent_stream_poll_seconds
