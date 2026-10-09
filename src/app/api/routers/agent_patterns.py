from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.observed_patterns import (
    ObservedPatternOut,
    PatternReviewRequest,
    PatternStatusCountsOut,
)
from app.services.observed_patterns import (
    PatternNotFound,
    count_patterns,
    count_patterns_by_status,
    list_patterns,
    review_pattern,
)

router = APIRouter(
    prefix="/agent/patterns",
    tags=["agent_patterns"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("", response_model=Page[ObservedPatternOut])
def list_observed_patterns(
    status: str | None = None,
    direction: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[ObservedPatternOut]:
    try:
        patterns, next_cursor = list_patterns(
            session, status=status, direction=direction, cursor=cursor, limit=limit
        )
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[ObservedPatternOut.model_validate(row) for row in patterns],
        next_cursor=next_cursor,
        total_count=count_patterns(session, status=status, direction=direction),
    )


@router.get("/counts", response_model=PatternStatusCountsOut)
def count_observed_patterns_by_status(
    session: Session = Depends(get_session),
) -> PatternStatusCountsOut:
    counts = count_patterns_by_status(session)
    return PatternStatusCountsOut(counts_by_status=counts, total_count=sum(counts.values()))


@router.post("/{pattern_id}/review", response_model=ObservedPatternOut)
def review_observed_pattern(
    pattern_id: int,
    body: PatternReviewRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> ObservedPatternOut:
    try:
        pattern = review_pattern(session, pattern_id, status=body.status, reviewed_by=reviewer_id)
        session.commit()
    except PatternNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="pattern not found") from None
    return ObservedPatternOut.model_validate(pattern)
