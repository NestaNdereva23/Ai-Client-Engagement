"""Digest endpoints: today's digest for an account manager or a fund."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.digest import DigestGroupOut
from app.services.digest import DigestNotFoundToday, get_today_digest_group

router = APIRouter(prefix="/digest", tags=["digest"])


@router.get("/{fa_or_fund_key}", response_model=DigestGroupOut)
def get_digest(fa_or_fund_key: str, session: Session = Depends(get_session)) -> DigestGroupOut:
    """Today's persisted digest lines for one group.

    fa_or_fund_key is "fa:<fa_id>" or "fund:<unit_fund_id>", matching how
    digest/build.py grouped the lines it wrote. A group with no at-risk
    clients today returns an empty list, not a 404 -- only a missing digest
    (no nightly run has completed yet today) is a 404.
    """
    try:
        view = get_today_digest_group(session, fa_or_fund_key)
    except DigestNotFoundToday:
        raise HTTPException(status_code=404, detail="no digest generated today") from None
    return DigestGroupOut.model_validate(view)
