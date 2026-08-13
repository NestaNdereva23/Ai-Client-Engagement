"""Risk endpoints: read-only queue views over the current risk state."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.risk import DustCleanupLineOut, RiskCoverageOut
from app.services.risk import book_coverage, list_dust_cleanup_queue

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/coverage", response_model=RiskCoverageOut)
def get_risk_coverage(session: Session = Depends(get_session)) -> RiskCoverageOut:
    """The active book's size vs. how many of them the last completed
    nightly run actually scored, an ops-facing read only.
    """
    return RiskCoverageOut.model_validate(book_coverage(session))


@router.get("/queues/dust_cleanup", response_model=Page[DustCleanupLineOut])
def get_dust_cleanup_queue(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[DustCleanupLineOut]:
    """The current dust_cleanup population, an ops-facing read only.

    No send capability sits anywhere near this route: nothing in campaigns/
    ever reads it, and this endpoint itself has no write action attached to
    it either.
    """
    try:
        rows, next_cursor = list_dust_cleanup_queue(session, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[DustCleanupLineOut.model_validate(r) for r in rows], next_cursor=next_cursor)
