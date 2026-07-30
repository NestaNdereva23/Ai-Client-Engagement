"""Business rules console: browse versions, and dry-run a feature tuple."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.rules.engine import NoRuleMatched
from app.schemas.rules import RulePreviewOut, RulePreviewRequest, RuleVersionOut
from app.services.rules import list_rule_versions, preview

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("", response_model=list[RuleVersionOut])
def get_rule_versions(
    version: int | None = None,
    active_on: date | None = None,
    session: Session = Depends(get_session),
) -> list[RuleVersionOut]:
    """One row per rule-set version: its validity window and rule count."""
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


@router.post("/preview", response_model=RulePreviewOut)
def preview_rules(
    body: RulePreviewRequest, session: Session = Depends(get_session)
) -> RulePreviewOut:
    """Dry-run a feature tuple: which angle, variant, and tier it resolves to."""
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
