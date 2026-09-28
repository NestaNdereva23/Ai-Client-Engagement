from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.config import get_settings
from app.db.session import get_session
from app.editable_settings import SettingValueError
from app.schemas.app_settings import AppSettingsOut, AppSettingsUpdate
from app.services.app_settings import list_app_settings, update_app_settings

router = APIRouter(prefix="/app-settings", tags=["app-settings"])


@router.get("", response_model=AppSettingsOut)
def get_app_settings(
    session: Session = Depends(get_session),
    _reviewer_id: str = Depends(get_current_reviewer_id),
) -> AppSettingsOut:
    return list_app_settings(session)


@router.put("", response_model=AppSettingsOut)
def put_app_settings(
    body: AppSettingsUpdate,
    session: Session = Depends(get_session),
    reviewer_id: str = Depends(get_current_reviewer_id),
) -> AppSettingsOut:
    try:
        update_app_settings(
            session,
            changes=body.changes,
            reason=body.reason,
            confirm_live=body.confirm_live,
            actor_id=reviewer_id,
        )
    except SettingValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    get_settings.reload_overrides()
    return list_app_settings(session)
