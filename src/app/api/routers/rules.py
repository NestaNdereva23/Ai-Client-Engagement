from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.rules.engine import NoRuleMatched
from app.schemas.rules import AngleStatusOut, RulePreviewOut, RulePreviewRequest, RuleVersionOut
from app.services.rules import list_angle_status, list_rule_versions, preview

router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(get_current_reviewer_id)])


@router.get("", response_model=list[RuleVersionOut])
def get_rule_versions(
    version: int | None = None,
    active_on: date | None = None,
    session: Session = Depends(get_session),
) -> list[RuleVersionOut]:
    today = date.today()
    rows = list_rule_versions(session, version=version, active_on=active_on)
    return [
        RuleVersionOut(
            version=v,
            valid_from=valid_from,
            valid_to=valid_to,
            rule_count=count,
            is_active=(valid_from <= today and (valid_to is None or valid_to > today)),
        )
        for v, valid_from, valid_to, count in rows
    ]


@router.get("/angles", response_model=list[AngleStatusOut])
def get_angle_status(
    active_on: date | None = None, session: Session = Depends(get_session)
) -> list[AngleStatusOut]:
    resolved_on = active_on or date.today()
    rows = list_angle_status(session, active_on=resolved_on)
    return [
        AngleStatusOut(angle=a, version=v, valid_from=vf, valid_to=vt, held=held)
        for a, v, vf, vt, held in rows
    ]


@router.post("/preview", response_model=RulePreviewOut)
def preview_rules(
    body: RulePreviewRequest, session: Session = Depends(get_session)
) -> RulePreviewOut:
    features = body.model_dump(exclude={"at"}, exclude_none=True)
    at = body.at or date.today()
    try:
        resolution = preview(session, features, at=at)
    except NoRuleMatched:
        raise HTTPException(status_code=422, detail="no rule matched the given features") from None
    return RulePreviewOut(
        message_angle=resolution.message_angle,
        urgency=resolution.urgency,
        priority_tier=resolution.priority_tier,
        prompt_variant=resolution.prompt_variant,
        rule_id=resolution.rule_id,
        rule_name=resolution.rule_name,
        version=resolution.version,
    )
