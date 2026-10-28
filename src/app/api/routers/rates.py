from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.rules.verified_rate import get_verified_rate, latest_verified_rate, record_verified_rate
from app.schemas.rates import VerifiedRateIn, VerifiedRateOut

router = APIRouter(prefix="/rates", tags=["rates"])


def _out(session: Session, row) -> VerifiedRateOut:
    return VerifiedRateOut(
        product=row.product,
        rate_value=float(row.rate_value),
        source=row.source,
        recorded_by=row.recorded_by,
        as_of=row.as_of,
        is_fresh=get_verified_rate(session, row.product) is not None,
    )


@router.get("/{product}", response_model=VerifiedRateOut)
def get_rate(
    product: str,
    session: Session = Depends(get_session),
    _reviewer_id: str = Depends(get_current_reviewer_id),
) -> VerifiedRateOut:
    row = latest_verified_rate(session, product)
    if row is None:
        raise HTTPException(status_code=404, detail="no verified rate is on file for this product")
    return _out(session, row)


@router.put("/{product}", response_model=VerifiedRateOut)
def set_rate(
    product: str,
    body: VerifiedRateIn,
    session: Session = Depends(get_session),
    reviewer_id: str = Depends(get_current_reviewer_id),
) -> VerifiedRateOut:
    row = record_verified_rate(
        session,
        product=product,
        rate_value=body.rate_value,
        source=body.source,
        recorded_by=reviewer_id,
    )
    session.commit()
    return _out(session, row)
