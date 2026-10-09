from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.action_catalog import (
    ActionCatalogValidationError,
    active_action_catalog_version,
    load_active_actions,
    set_action_content_mix,
)
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.schemas.action_catalog import (
    ActionMixListOut,
    ActionMixOut,
    SetContentMixIn,
    SetContentMixOut,
)

router = APIRouter(
    prefix="/agent/actions",
    tags=["agent_actions"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("", response_model=ActionMixListOut)
def list_action_mixes(session: Session = Depends(get_session)) -> ActionMixListOut:
    today = date.today()
    version = active_action_catalog_version(session, today)
    rows = load_active_actions(session, today)
    actions = [
        ActionMixOut(
            action_code=row.action_code,
            title=row.title,
            content_mix=row.content_mix,
            default_permission=row.default_permission,
            response_kind=row.response_kind,
            sends_message=bool(row.message_angle),
            paused=row.paused,
            version=row.version,
        )
        for row in sorted(rows.values(), key=lambda row: row.action_code)
    ]
    return ActionMixListOut(version=version, actions=actions)


@router.post("/{action_code}/content-mix", response_model=SetContentMixOut)
def set_content_mix(
    action_code: str,
    body: SetContentMixIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SetContentMixOut:
    try:
        version = set_action_content_mix(
            session, action_code, body.content_mix, changed_by=reviewer_id
        )
    except ActionCatalogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return SetContentMixOut(action_code=action_code, content_mix=body.content_mix, version=version)
