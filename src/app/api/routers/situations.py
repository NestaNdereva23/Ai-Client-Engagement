from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.schemas.situations import SituationDeltaOut
from app.services.situations import SituationNotFound, situation_delta_summary

router = APIRouter(
    prefix="/agent/situations",
    tags=["situations"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("/{situation_code}/delta", response_model=SituationDeltaOut)
def get_situation_delta(
    situation_code: str, session: Session = Depends(get_session)
) -> SituationDeltaOut:
    try:
        summary = situation_delta_summary(session, situation_code)
    except SituationNotFound:
        raise HTTPException(status_code=404, detail="situation not found") from None

    return SituationDeltaOut(**summary)
