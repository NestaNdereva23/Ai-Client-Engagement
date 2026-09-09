"""Where a run says what it is doing, as it does it.

An event is written first and shown second, so watching a run live and
opening a finished one read the same rows in the same order.

Writing one never slows a run down and never fails it. The run hands the
event over and carries straight on: the place in the queue is claimed in
memory under a lock, so groups working side by side never claim the same
one, and a thread of its own puts the rows in the database on its own
session. A run's own transaction is never involved, which is what lets the
last thing a failing run says survive the rollback that follows it.

Nothing in here raises into the run. An event that cannot be written is
logged and dropped.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.agent_event import TOOL_COMPLETED, WARNING, AgentEvent
from app.db.session import SessionLocal
from app.privacy.scanners import OutboundLeak, scan_outbound

logger = structlog.get_logger(__name__)

WRITE_BATCH_SIZE = 50

QUEUE_LIMIT = 5000

CLOSE_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class PendingEvent:
    """One event, waiting its turn to be written."""

    ordinal: int
    kind: str
    detail: dict[str, Any]
    created_at: datetime


def stored_detail(kind: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    """The detail as it will be stored, checked the way tool output is.

    An event carries counts, group names and step names, never a client's
    name or a live contact channel. One that somehow does is stored
    withheld rather than kept.
    """
    rendered = json.dumps(dict(detail), sort_keys=True, default=str)
    try:
        scan_outbound(rendered)
    except OutboundLeak:
        logger.error("agent_event.detail_withheld", kind=kind)
        return {"withheld": True}
    return json.loads(rendered)


class EventLog:
    """Somewhere a run's events go. This one throws them away.

    It is the default everywhere, so no caller needs to check whether a run
    is being recorded before it says what it is doing.
    """

    def record(self, kind: str, /, **detail: Any) -> None:
        return None

    def close(self) -> None:
        return None


NO_EVENTS = EventLog()


class RunEventLog(EventLog):
    """The events of one run, written by a thread of its own."""

    def __init__(
        self,
        run_id: int,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        start_ordinal: int = 1,
    ) -> None:
        self.run_id = run_id
        self._session_factory = session_factory
        self._next_ordinal = start_ordinal
        self._lock = threading.Lock()
        self._queue: queue.Queue[PendingEvent | None] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._writer = threading.Thread(
            target=self._drain, name=f"agent-events-{run_id}", daemon=True
        )
        self._writer.start()

    def record(self, kind: str, /, **detail: Any) -> None:
        with self._lock:
            ordinal = self._next_ordinal
            self._next_ordinal += 1
        pending = PendingEvent(
            ordinal=ordinal, kind=kind, detail=detail, created_at=datetime.now(UTC)
        )
        try:
            self._queue.put_nowait(pending)
        except queue.Full:
            logger.warning("agent_event.dropped", run_id=self.run_id, kind=kind, ordinal=ordinal)

    def close(self) -> None:
        """Stop the writer and wait for what is already queued to land."""
        self._queue.put(None)
        self._writer.join(timeout=CLOSE_WAIT_SECONDS)
        if self._writer.is_alive():
            logger.warning("agent_event.writer_still_running", run_id=self.run_id)

    def _drain(self) -> None:
        session = self._session_factory()
        try:
            while True:
                batch, stopping = self._next_batch()
                if batch:
                    self._write(session, batch)
                if stopping:
                    return
        finally:
            session.close()

    def _next_batch(self) -> tuple[list[PendingEvent], bool]:
        first = self._queue.get()
        if first is None:
            return [], True
        batch = [first]
        while len(batch) < WRITE_BATCH_SIZE:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                return batch, False
            if nxt is None:
                return batch, True
            batch.append(nxt)
        return batch, False

    def _write(self, session: Session, batch: list[PendingEvent]) -> None:
        try:
            session.add_all(
                AgentEvent(
                    run_id=self.run_id,
                    ordinal=pending.ordinal,
                    kind=pending.kind,
                    detail=stored_detail(pending.kind, pending.detail),
                    created_at=pending.created_at,
                )
                for pending in batch
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.warning(
                "agent_event.write_failed",
                run_id=self.run_id,
                kinds=[pending.kind for pending in batch],
                exc_info=True,
            )


def record_event(session: Session, *, run_id: int, kind: str, **detail: Any) -> None:
    """Write one event on the caller's own session.

    This is for something that happens to a run's work long after the run
    itself ended, such as a person deciding what to do with a finding. It
    takes its place in the queue from what is already stored, since the run
    that wrote the rest of them is over.

    Nothing here raises. An event that cannot be written is logged and
    dropped rather than failing the thing it was recording.
    """
    try:
        with session.begin_nested():
            highest = session.scalar(
                select(func.coalesce(func.max(AgentEvent.ordinal), 0)).where(
                    AgentEvent.run_id == run_id
                )
            )
            session.add(
                AgentEvent(
                    run_id=run_id,
                    ordinal=int(highest or 0) + 1,
                    kind=kind,
                    detail=stored_detail(kind, detail),
                )
            )
    except Exception:
        logger.warning("agent_event.write_failed", run_id=run_id, kind=kind, exc_info=True)


def tool_finished(
    events: EventLog, *, tool_name: str, ordinal: int, output: Mapping[str, Any]
) -> None:
    """The events one finished tool call writes, however it ended.

    A tool that refused says so twice: once as the call finishing, which is
    what a replay reads in order, and once as a warning, which is what a
    person scanning for trouble reads instead.
    """
    error = output.get("error")
    events.record(TOOL_COMPLETED, tool_name=tool_name, ordinal=ordinal, error=error)
    if error:
        events.record(WARNING, about="tool_call", tool_name=tool_name, ordinal=ordinal, error=error)
