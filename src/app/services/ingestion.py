"""Console-facing reads and the trigger for an ingestion run.

trigger_run only assigns a run id and hands the actual pull to a background
task; the worker itself (workers/ingestion.py) already checkpoints per page,
so a run started here is resumable and safe to observe mid-flight through
list_runs and get_run while it is still going.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.db.models.models import IngestionStatus
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_cursor, encode_cursor
from app.workers.ingestion import IngestionWorker


class RunNotFound(Exception):
    """No ingestion_status row exists with the given run id."""


def run_in_background(client: Any, *, run_id: str, endpoint: str, max_pages: int) -> None:
    """Run one ingestion pass and always release the client afterward."""
    worker = IngestionWorker(client, endpoint=endpoint, max_pages=max_pages)
    try:
        worker.run(run_id=run_id)
    finally:
        client.close()


def resolve_trigger_run_id(session: Session, requested_run_id: str | None) -> str:
    """The run id a trigger endpoint should use: fresh, or the caller's own id
    when it names a run to resume. Shared by the console and integration
    trigger endpoints so a typo or an unedited placeholder can never silently
    start a fresh run under that name; raises RunNotFound when requested_run_id
    is given but no such run exists.
    """
    if requested_run_id is None:
        return uuid.uuid4().hex
    get_run(session, requested_run_id)
    return requested_run_id


def list_runs(
    session: Session, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> tuple[list[IngestionStatus], str | None]:
    """Runs newest first, one page at a time."""
    limit = clamp_limit(limit)
    query = select(IngestionStatus)
    if cursor is not None:
        before_started_at, before_id = decode_cursor(cursor)
        query = query.where(
            tuple_(IngestionStatus.started_at, IngestionStatus.run_id)
            < (before_started_at, before_id)
        )
    query = query.order_by(IngestionStatus.started_at.desc(), IngestionStatus.run_id.desc()).limit(
        limit + 1
    )
    rows = list(session.scalars(query).all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.started_at, last.run_id)
    return rows, next_cursor


def get_run(session: Session, run_id: str) -> IngestionStatus:
    """One run, or raise RunNotFound."""
    run = session.get(IngestionStatus, run_id)
    if run is None:
        raise RunNotFound(run_id)
    return run
