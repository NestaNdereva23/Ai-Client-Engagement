"""Who is asking, and whose book they may see.

Ticketing proves who the person is. This module decides what they may see.
Every route that returns client data takes get_access, and uses the advisor it
names. A value sent by the browser is never trusted over it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.reviewer_auth import bearer_scheme, decode_bearer_token
from app.db.models.fa_assignment import FaAssignment
from app.db.models.user_access import ROLE_ADMIN, UserAccess, normalize_email
from app.db.session import get_session
from app.services.user_access import advisor_fa_id, find_access


@dataclass(frozen=True)
class Access:
    """email is the real person. shown_email is the advisor whose book is shown:
    their own for an advisor, the viewed one for an admin who has set view as,
    and empty for an admin who is looking at every book. ai_assistant is true
    for every admin, and for an advisor only when it has been switched on."""

    email: str
    is_admin: bool
    shown_email: str | None
    shown_fa_id: str | None
    ai_assistant: bool

    @property
    def sees_every_book(self) -> bool:
        return self.is_admin and self.shown_email is None

    @property
    def on_behalf_of(self) -> str | None:
        return self.shown_email if self.is_admin else None


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def get_access(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> Access:
    email = decode_bearer_token(credentials).get("email")
    if not email:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    row = find_access(session, email)
    if row is None or not row.active:
        raise _forbidden("this account has no access to client data")
    return access_for(session, row)


def access_for(session: Session, row: UserAccess) -> Access:
    if row.role != ROLE_ADMIN:
        return Access(
            row.email, False, row.email, advisor_fa_id(row), ai_assistant=row.ai_assistant
        )
    if not row.view_as_email:
        return Access(row.email, True, None, None, ai_assistant=True)
    viewed = find_access(session, row.view_as_email)
    viewed_fa_id = advisor_fa_id(viewed) if viewed is not None else None
    return Access(
        row.email, True, normalize_email(row.view_as_email), viewed_fa_id, ai_assistant=True
    )


def get_admin_access(access: Access = Depends(get_access)) -> Access:
    if not access.is_admin:
        raise _forbidden("only an admin can do this")
    return access


def require_ai_assistant(access: Access = Depends(get_access)) -> Access:
    if not access.ai_assistant:
        raise _forbidden("the AI Assistant is not switched on for this account")
    return access


def book_fa_id(access: Access, requested: str | None = None) -> str | None:
    """The advisor id whose clients this call may list.

    An advisor, or an admin who is viewing as one, always gets that advisor's id
    and the requested value is ignored. An admin looking at every book keeps the
    requested value, or none to mean no filter."""
    if access.sees_every_book:
        return requested
    if access.shown_fa_id is None:
        raise _forbidden("no client book is linked to this account yet")
    return access.shown_fa_id


def require_client_in_book(
    client_id: int,
    unit_fund_id: int,
    access: Access = Depends(get_access),
    session: Session = Depends(get_session),
) -> Access:
    """Refuse a client that is not in the shown advisor's book."""
    if access.sees_every_book:
        return access
    owner = session.scalar(
        select(FaAssignment.fa_id).where(
            FaAssignment.client_id == client_id, FaAssignment.unit_fund_id == unit_fund_id
        )
    )
    if access.shown_fa_id is None or owner != access.shown_fa_id:
        raise _forbidden("this client is not in your book")
    return access
