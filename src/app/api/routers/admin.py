"""Admin dashboards: run metrics and guardrail failure rates, sliced by angle,
tier, prompt version, and model version.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.admin_metrics import GuardrailFailureOut, RunMetricsOut
from app.services.admin_metrics import guardrail_failure_rates, run_metrics

router = APIRouter(tags=["admin"])


@router.get("/admin/metrics/runs", response_model=list[RunMetricsOut])
def get_run_metrics(
    angle: str | None = None,
    tier: str | None = None,
    prompt_variant: str | None = None,
    model_id: str | None = None,
    session: Session = Depends(get_session),
) -> list[RunMetricsOut]:
    """Tokens, latency, cost per message, and error rate, grouped by angle,
    tier, prompt version, and model version.
    """
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
    """Per-angle guardrail failure rate: which guardrail keeps failing which brief."""
    rows = guardrail_failure_rates(session, message_angle=angle)
    return [GuardrailFailureOut.model_validate(row) for row in rows]
