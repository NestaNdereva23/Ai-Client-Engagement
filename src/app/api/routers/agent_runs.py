"""HTTP surface for starting and reading back agent runs."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.agent_loop import AgentRunInProgress
from app.api.reviewer_auth import get_current_reviewer_id
from app.config import get_settings
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.agent_runs import AgentRunOut
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
