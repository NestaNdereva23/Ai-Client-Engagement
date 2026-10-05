from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.chat import ChatSessionNotFound, run_chat_in_background, start_chat_turn
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.agent_chat import ChatAskIn, ChatAskOut, ChatSessionOut
from app.services.agent_chat import get_chat_session, list_chat_sessions

router = APIRouter(
    prefix="/agent/chat",
    tags=["agent_chat"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.post("", response_model=ChatAskOut, status_code=202)
def post_chat_question(
    body: ChatAskIn,
    background_tasks: BackgroundTasks,
    as_of: date | None = None,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> ChatAskOut:
    try:
        turn = start_chat_turn(
            session,
            question=body.question,
            session_id=body.session_id,
            reviewer_id=reviewer_id,
            as_of=as_of,
        )
    except ChatSessionNotFound:
        raise HTTPException(status_code=404, detail="chat session not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    background_tasks.add_task(run_chat_in_background, turn.turn_id, as_of=as_of)
    return ChatAskOut(session_id=turn.session_id, turn_id=turn.turn_id, run_id=turn.run_id)


@router.get("/sessions", response_model=Page[ChatSessionOut])
def get_chat_sessions(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[ChatSessionOut]:
    try:
        sessions, next_cursor = list_chat_sessions(session, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[ChatSessionOut.model_validate(row) for row in sessions],
        next_cursor=next_cursor,
    )


@router.get("/sessions/{session_id}", response_model=ChatSessionOut)
def get_chat_session_route(
    session_id: int, session: Session = Depends(get_session)
) -> ChatSessionOut:
    try:
        return get_chat_session(session, session_id)
    except ChatSessionNotFound:
        raise HTTPException(status_code=404, detail="chat session not found") from None
