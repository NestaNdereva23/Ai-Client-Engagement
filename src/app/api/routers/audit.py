from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.audit import AuditLogEntryOut, TraceOut
from app.services.audit import TraceNotFound, browse_audit_log, get_trace

router = APIRouter(tags=["audit"], dependencies=[Depends(get_current_reviewer_id)])


@router.get("/audit", response_model=Page[AuditLogEntryOut])
def list_audit_log(
    entity: str | None = None,
    run: str | None = None,
    trace: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[AuditLogEntryOut]:
    try:
        rows, next_cursor = browse_audit_log(
            session, entity_type=entity, run_id=run, trace_id=trace, cursor=cursor, limit=limit
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[AuditLogEntryOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/traces/{trace_id}", response_model=TraceOut)
def get_trace_ref(trace_id: str, session: Session = Depends(get_session)) -> TraceOut:
    try:
        trace = get_trace(session, trace_id)
    except TraceNotFound:
        raise HTTPException(status_code=404, detail="trace not found") from None
    return TraceOut.model_validate(trace)
