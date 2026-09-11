"""HTTP surface for starting and reading back agent runs."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.agent_loop import AgentRunInProgress
from app.api.reviewer_auth import get_current_reviewer_id
from app.config import get_settings
from app.db.async_session import AsyncSessionLocal
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.agent_events import AgentEventOut, RunActivityOut, RunProgressOut
from app.schemas.agent_runs import AgentRunOut
from app.services.agent_event_stream import (
    STREAM_HEADERS,
    StreamSlots,
    resume_point,
    stream_run_events,
)
from app.services.agent_events import (
    EVENT_PAGE_LIMIT,
    last_ordinal,
    list_run_events,
    run_progress,
    run_state_async,
)
from app.services.agent_runs import (
    AgentRunNotFound,
    get_agent_run,
    list_agent_runs,
    run_agent_in_background,
    start_manual_run,
)

router = APIRouter(
    prefix="/agent/runs",
    tags=["agent_runs"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.post("", response_model=AgentRunOut, status_code=202)
def post_agent_run(
    background_tasks: BackgroundTasks,
    as_of: date | None = None,
    session: Session = Depends(get_session),
) -> AgentRunOut:
    try:
        run = start_manual_run(session, as_of=as_of)
        session.commit()
    except AgentRunInProgress as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None

    background_tasks.add_task(
        run_agent_in_background, run.run_id, as_of=as_of, settings=get_settings()
    )
    return AgentRunOut.model_validate(run)


@router.get("/{run_id}", response_model=AgentRunOut)
def get_agent_run_route(run_id: int, session: Session = Depends(get_session)) -> AgentRunOut:
    try:
        run = get_agent_run(session, run_id)
    except AgentRunNotFound:
        raise HTTPException(status_code=404, detail="agent run not found") from None
    return AgentRunOut.model_validate(run)


open_streams = StreamSlots(limit=get_settings().agent_stream_max_open)


@router.get("/{run_id}/events/stream")
async def get_agent_run_event_stream(
    run_id: int,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    settings = get_settings()
    open_streams.limit = settings.agent_stream_max_open

    async with AsyncSessionLocal() as session:
        state = await run_state_async(session, run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="agent run not found")

    if not open_streams.take():
        raise HTTPException(status_code=503, detail="too many people are watching runs right now")

    return StreamingResponse(
        stream_run_events(
            run_id,
            after=resume_point(after, last_event_id),
            settings=settings,
            slots=open_streams,
        ),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


@router.get("/{run_id}/events", response_model=RunActivityOut)
def get_agent_run_events(
    run_id: int,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=EVENT_PAGE_LIMIT, ge=1, le=EVENT_PAGE_LIMIT),
    session: Session = Depends(get_session),
) -> RunActivityOut:
    try:
        run = get_agent_run(session, run_id)
    except AgentRunNotFound:
        raise HTTPException(status_code=404, detail="agent run not found") from None

    events = list_run_events(session, run_id, after=after, limit=limit)
    return RunActivityOut(
        run_id=run_id,
        state=run.state,
        events=[AgentEventOut.model_validate(event) for event in events],
        last_ordinal=last_ordinal(session, run_id),
        progress=RunProgressOut.model_validate(run_progress(session, run_id)),
    )


@router.get("", response_model=Page[AgentRunOut])
def get_agent_runs(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[AgentRunOut]:
    try:
        runs, next_cursor = list_agent_runs(session, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[AgentRunOut.model_validate(run) for run in runs],
        next_cursor=next_cursor,
    )
