from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agents.guide_mix import MIX_LABELS
from app.agents.results_summary import read_results_by_mix
from app.api.reviewer_auth import get_current_reviewer_id
from app.config import get_settings
from app.db.session import get_session
from app.schemas.agent_metrics import (
    CostOut,
    CoverageOut,
    DashboardOut,
    FreedomOut,
    GrowthFoundOut,
    GuideMixResultLine,
    GuideMixResultsOut,
    LossesPreventedOut,
    MoneyAtRiskOut,
    QualityOut,
    SpeedOut,
    SystemSpeedOut,
    UsefulnessOut,
)
from app.services import agent_metrics

router = APIRouter(
    prefix="/agent/metrics",
    tags=["agent_metrics"],
    dependencies=[Depends(get_current_reviewer_id)],
)


def _window(
    since: date | None = None,
    until: date | None = None,
    window_days: int | None = Query(default=None, ge=1),
):
    return agent_metrics.resolve_window(since, until, window_days)


@router.get("/speed", response_model=SpeedOut)
def get_speed(window=Depends(_window), session: Session = Depends(get_session)) -> SpeedOut:
    return agent_metrics.speed_metric(session, window)


@router.get("/coverage", response_model=CoverageOut)
def get_coverage(window=Depends(_window), session: Session = Depends(get_session)) -> CoverageOut:
    return agent_metrics.coverage_metric(session, window)


@router.get("/money-at-risk", response_model=MoneyAtRiskOut)
def get_money_at_risk(
    window=Depends(_window), session: Session = Depends(get_session)
) -> MoneyAtRiskOut:
    return agent_metrics.money_at_risk_metric(session, window)


@router.get("/losses-prevented", response_model=LossesPreventedOut)
def get_losses_prevented(
    window=Depends(_window), session: Session = Depends(get_session)
) -> LossesPreventedOut:
    return agent_metrics.losses_prevented_metric(session, window)


@router.get("/growth", response_model=GrowthFoundOut)
def get_growth(window=Depends(_window), session: Session = Depends(get_session)) -> GrowthFoundOut:
    return agent_metrics.growth_found_metric(session, window)


@router.get("/quality", response_model=QualityOut)
def get_quality(window=Depends(_window), session: Session = Depends(get_session)) -> QualityOut:
    return agent_metrics.quality_metric(session, window)


@router.get("/usefulness", response_model=UsefulnessOut)
def get_usefulness(
    window=Depends(_window), session: Session = Depends(get_session)
) -> UsefulnessOut:
    return agent_metrics.usefulness_metric(session, window)


@router.get("/freedom", response_model=FreedomOut)
def get_freedom(window=Depends(_window), session: Session = Depends(get_session)) -> FreedomOut:
    return agent_metrics.freedom_metric(session, window)


@router.get("/cost", response_model=CostOut)
def get_cost(window=Depends(_window), session: Session = Depends(get_session)) -> CostOut:
    return agent_metrics.cost_metric(session, window)


@router.get("/system-speed", response_model=SystemSpeedOut)
def get_system_speed(
    window=Depends(_window), session: Session = Depends(get_session)
) -> SystemSpeedOut:
    return agent_metrics.system_speed_metric(session, window)


@router.get("/guide-mix", response_model=GuideMixResultsOut)
def get_guide_mix(
    weeks: int = Query(default=1, ge=1, le=52),
    session: Session = Depends(get_session),
) -> GuideMixResultsOut:
    period_hours = get_settings().action_performance_period_hours
    results = read_results_by_mix(session, lookback_hours=weeks * period_hours)
    return GuideMixResultsOut(
        window_days=results.window_days,
        lookback_hours=results.lookback_hours,
        min_group_size=results.min_group_size,
        weeks=weeks,
        lines=[
            GuideMixResultLine(
                guide_mix=line.guide_mix,
                label=MIX_LABELS.get(line.guide_mix, line.guide_mix),
                sent=line.sent_count,
                reply_percent=line.reply_percent,
                opt_out_percent=line.opt_out_percent,
                edit_percent=line.edit_percent,
                deposit_percent=line.deposit_percent,
                money_in_kes=line.money_in_kes,
            )
            for line in results.lines
        ],
        withheld_small_groups=results.withheld_small_groups,
    )


@router.get("", response_model=DashboardOut)
def get_dashboard(window=Depends(_window), session: Session = Depends(get_session)) -> DashboardOut:
    return agent_metrics.dashboard(session, window)
