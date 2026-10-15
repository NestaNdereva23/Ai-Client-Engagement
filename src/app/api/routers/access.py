from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.access import Access, access_for, get_access, get_admin_access
from app.db.session import get_session
from app.schemas.access import AccessOut, AdvisorOut, ViewAsRequest
from app.services.user_access import (
    NotAnAdvisor,
    advisor_fa_id,
    clear_view_as,
    list_active_advisors,
    set_view_as,
)

router = APIRouter(prefix="/access", tags=["access"])


def _access_out(access: Access) -> AccessOut:
    return AccessOut(
        email=access.email,
        role="admin" if access.is_admin else "advisor",
        fa_id=access.shown_fa_id,
        viewing_as=access.shown_email if access.is_admin else None,
    )


@router.get("/me", response_model=AccessOut)
def get_my_access(access: Access = Depends(get_access)) -> AccessOut:
    return _access_out(access)


@router.get("/advisors", response_model=list[AdvisorOut])
def get_advisors(
    _admin: Access = Depends(get_admin_access), session: Session = Depends(get_session)
) -> list[AdvisorOut]:
    return [
        AdvisorOut(email=row.email, name=row.name or row.email, fa_id=advisor_fa_id(row))
        for row in list_active_advisors(session)
    ]


@router.put("/view-as", response_model=AccessOut)
def put_view_as(
    body: ViewAsRequest,
    admin: Access = Depends(get_admin_access),
    session: Session = Depends(get_session),
) -> AccessOut:
    try:
        row = set_view_as(session, admin.email, body.email)
    except NotAnAdvisor:
        session.rollback()
        raise HTTPException(status_code=404, detail="no active advisor with that email") from None
    session.commit()
    return _access_out(access_for(session, row))


@router.delete("/view-as", response_model=AccessOut)
def delete_view_as(
    admin: Access = Depends(get_admin_access), session: Session = Depends(get_session)
) -> AccessOut:
    row = clear_view_as(session, admin.email)
    session.commit()
    return _access_out(access_for(session, row))
