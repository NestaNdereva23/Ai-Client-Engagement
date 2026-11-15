from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.client_assistant import run_assistant_turn
from app.api.access import Access, require_client_in_book
from app.api.errors import ApiError
from app.db.session import get_session
from app.llmops.tracing import get_shared_tracer
from app.schemas.client_assistant import (
    AssistantAboutOut,
    AssistantSessionOut,
    AssistantSessionSummaryOut,
    StarterOut,
    TurnIn,
    TurnOut,
)
from app.services.client_assistant import (
    DEFAULT_SESSION_LIMIT,
    AssistantSessionNotFound,
    BadTurnRequest,
    StarterNotFound,
    StarterUnavailable,
    assistant_about,
    get_client_session,
    list_client_sessions,
    list_starters,
    start_client_turn,
)

router = APIRouter(prefix="/briefing/{client_id}/{unit_fund_id}/assistant", tags=["assistant"])


@router.get("/starters", response_model=list[StarterOut])
def get_starters(
    client_id: int,
    unit_fund_id: int,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> list[StarterOut]:
    return list_starters(session, client_id, unit_fund_id)


@router.get("/about", response_model=AssistantAboutOut)
def get_about(
    client_id: int,
    unit_fund_id: int,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> AssistantAboutOut:
    return assistant_about(session, client_id, unit_fund_id)


@router.get("/sessions", response_model=list[AssistantSessionSummaryOut])
def get_sessions(
    client_id: int,
    unit_fund_id: int,
    limit: int = Query(default=DEFAULT_SESSION_LIMIT, ge=1, le=100),
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> list[AssistantSessionSummaryOut]:
    return list_client_sessions(
        session,
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        username=access.email,
        sees_every_book=access.sees_every_book,
        limit=limit,
    )


@router.get("/sessions/{session_id}", response_model=AssistantSessionOut)
def get_session_with_turns(
    client_id: int,
    unit_fund_id: int,
    session_id: int,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> AssistantSessionOut:
    try:
        return get_client_session(
            session,
            session_id,
            client_id=client_id,
            unit_fund_id=unit_fund_id,
            username=access.email,
            sees_every_book=access.sees_every_book,
        )
    except AssistantSessionNotFound:
        raise HTTPException(status_code=404, detail="chat session not found") from None


@router.post("/turns", response_model=TurnOut, status_code=202)
def post_turn(
    client_id: int,
    unit_fund_id: int,
    body: TurnIn,
    background_tasks: BackgroundTasks,
    access: Access = Depends(require_client_in_book),
    session: Session = Depends(get_session),
) -> TurnOut:
    try:
        turn = start_client_turn(
            session,
            client_id=client_id,
            unit_fund_id=unit_fund_id,
            username=access.email,
            sees_every_book=access.sees_every_book,
            fa_id=None if access.sees_every_book else access.shown_fa_id,
            session_id=body.session_id,
            starter_code=body.starter_code,
            action=body.action,
            text=body.text,
        )
    except AssistantSessionNotFound:
        raise HTTPException(status_code=404, detail="chat session not found") from None
    except StarterNotFound:
        raise HTTPException(status_code=404, detail="that starter does not exist") from None
    except StarterUnavailable as exc:
        raise ApiError(status_code=409, code="starter_unavailable", detail=exc.reason) from None
    except BadTurnRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    background_tasks.add_task(
        run_assistant_turn,
        turn.turn_id,
        username=access.email,
        on_behalf_of=access.on_behalf_of,
        sees_every_book=access.sees_every_book,
        tracer=get_shared_tracer(),
    )
    return TurnOut(session_id=turn.session_id, turn_id=turn.turn_id, run_id=turn.run_id)
