from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.risk import (
    RiskAnalyticsOut,
    RiskBucketOut,
    RiskCoverageOut,
    RiskTrendOut,
    RiskTrendPointOut,
    SmallBalanceReviewLineOut,
)
from app.services.risk import (
    book_coverage,
    list_small_balance_review_queue,
    risk_analytics,
    risk_trend,
)

router = APIRouter(prefix="/risk", tags=["risk"], dependencies=[Depends(get_current_reviewer_id)])


@router.get("/coverage", response_model=RiskCoverageOut)
def get_risk_coverage(session: Session = Depends(get_session)) -> RiskCoverageOut:
    return RiskCoverageOut.model_validate(book_coverage(session))


@router.get("/analytics", response_model=RiskAnalyticsOut)
def get_risk_analytics(session: Session = Depends(get_session)) -> RiskAnalyticsOut:
    analytics = risk_analytics(session)
    return RiskAnalyticsOut(
        book_size=analytics.book_size,
        scored_count=analytics.scored_count,
        as_of=analytics.as_of,
        by_risk_band=[RiskBucketOut(key=k, count=c) for k, c in analytics.by_risk_band],
        by_route=[RiskBucketOut(key=k, count=c) for k, c in analytics.by_route],
        by_balance_tier=[RiskBucketOut(key=k, count=c) for k, c in analytics.by_balance_tier],
        by_value_tier=[RiskBucketOut(key=k, count=c) for k, c in analytics.by_value_tier],
        by_recency_band=[RiskBucketOut(key=k, count=c) for k, c in analytics.by_recency_band],
        signal_frequency=[RiskBucketOut(key=k, count=c) for k, c in analytics.signal_frequency],
        primary_signal_distribution=[
            RiskBucketOut(key=k, count=c) for k, c in analytics.primary_signal_distribution
        ],
        total_fund_at_risk=analytics.total_fund_at_risk,
    )


@router.get("/analytics/trend", response_model=RiskTrendOut)
def get_risk_trend(
    runs: int = Query(default=30, ge=1, le=90),
    session: Session = Depends(get_session),
) -> RiskTrendOut:
    points = risk_trend(session, runs=runs)
    return RiskTrendOut(
        points=[
            RiskTrendPointOut(
                run_id=p.run_id,
                as_of=p.as_of,
                by_risk_band=[RiskBucketOut(key=k, count=c) for k, c in p.by_risk_band],
                total_fund_at_risk=p.total_fund_at_risk,
                avg_risk_score=p.avg_risk_score,
            )
            for p in points
        ]
    )


@router.get("/queues/small_balance_review", response_model=Page[SmallBalanceReviewLineOut])
def get_small_balance_review_queue(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[SmallBalanceReviewLineOut]:
    try:
        rows, next_cursor = list_small_balance_review_queue(session, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(
        items=[SmallBalanceReviewLineOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )
