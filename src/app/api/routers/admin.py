from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.schemas.admin_metrics import (
    DailyCountOut,
    FunnelOut,
    GuardrailFailureOut,
    JudgeScoreOut,
    RunMetricsOut,
)
from app.services.admin_metrics import (
    daily_generation_counts,
    funnel_counts,
    guardrail_failure_rates,
    judge_score_metrics,
    run_metrics,
)

router = APIRouter(tags=["admin"], dependencies=[Depends(get_current_reviewer_id)])


@router.get("/admin/metrics/runs", response_model=list[RunMetricsOut])
def get_run_metrics(
    angle: str | None = None,
    tier: str | None = None,
    prompt_variant: str | None = None,
    model_id: str | None = None,
    session: Session = Depends(get_session),
) -> list[RunMetricsOut]:
    rows = run_metrics(
        session,
        message_angle=angle,
        priority_tier=tier,
        prompt_variant=prompt_variant,
        model_id=model_id,
    )
    return [RunMetricsOut.model_validate(row) for row in rows]


@router.get("/admin/metrics/guardrail-failures", response_model=list[GuardrailFailureOut])
def get_guardrail_failure_rates(
    angle: str | None = None,
    session: Session = Depends(get_session),
) -> list[GuardrailFailureOut]:
    rows = guardrail_failure_rates(session, message_angle=angle)
    return [GuardrailFailureOut.model_validate(row) for row in rows]


@router.get("/admin/metrics/judge-scores", response_model=list[JudgeScoreOut])
def get_judge_score_metrics(
    angle: str | None = None,
    tier: str | None = None,
    session: Session = Depends(get_session),
) -> list[JudgeScoreOut]:
    rows = judge_score_metrics(session, message_angle=angle, priority_tier=tier)
    return [JudgeScoreOut.model_validate(row) for row in rows]


@router.get("/admin/metrics/funnel", response_model=FunnelOut)
def get_funnel_counts(session: Session = Depends(get_session)) -> FunnelOut:
    return FunnelOut.model_validate(funnel_counts(session))


@router.get("/admin/metrics/daily", response_model=list[DailyCountOut])
def get_daily_counts(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_session),
) -> list[DailyCountOut]:
    rows = daily_generation_counts(session, days=days)
    return [DailyCountOut.model_validate(row) for row in rows]
