from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.schemas.digest import DigestGroupOut
from app.services.digest import DigestNotFoundToday, get_today_digest_group

router = APIRouter(
    prefix="/digest", tags=["digest"], dependencies=[Depends(get_current_reviewer_id)]
)


@router.get("/{fa_or_fund_key}", response_model=DigestGroupOut)
def get_digest(fa_or_fund_key: str, session: Session = Depends(get_session)) -> DigestGroupOut:
    try:
        view = get_today_digest_group(session, fa_or_fund_key)
    except DigestNotFoundToday:
        raise HTTPException(status_code=404, detail="no digest generated today") from None
    return DigestGroupOut.model_validate(view)
