"""Session-backed login for the reviewer console.

Session state lives in the signed cookie SessionMiddleware manages (added
in app.main, keyed by settings.console_session_secret_key) -- only
user_id is stored there; everything else is re-read from the database on
each request, so a role change or deactivation takes effect on the very
next request rather than waiting for re-login.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.models.auth import ReviewerUser
from app.db.session import get_session

_SESSION_USER_ID_KEY = "reviewer_user_id"
LOGIN_PATH = "/reviewer/login"


def _redirect_to_login() -> HTTPException:
    """A 303 to the login page. Raised as an HTTPException rather than
    returned, since this runs as a dependency, but a 303 with Location
    redirects a browser the same way any other 303 response would.
    """
    return HTTPException(status_code=303, headers={"Location": LOGIN_PATH})


def login_user(request: Request, user: ReviewerUser) -> None:
    """Start a session for user."""
    request.session[_SESSION_USER_ID_KEY] = user.user_id


def logout_user(request: Request) -> None:
    """End the current session, if any."""
    request.session.clear()


def get_current_user(request: Request, session: Session = Depends(get_session)) -> ReviewerUser:
    """The logged-in reviewer_user, or redirect to the login page.

    Redirects (rather than 401s) for a missing session, an id that no
    longer resolves to a row, or an account marked inactive since login --
    a browser following the page should land on the login form, not a
    bare error page.
    """
    user_id = request.session.get(_SESSION_USER_ID_KEY)
    if user_id is None:
        raise _redirect_to_login()

    user = session.get(ReviewerUser, user_id)
    if user is None or not user.active:
        raise _redirect_to_login()
    return user


def require_role(*roles: str) -> Callable[..., ReviewerUser]:
    """A dependency admitting only a logged-in user whose role is in roles.

    403s rather than redirecting: the user is authenticated, just not
    permitted, and a redirect back to a page they'd 403 on again would loop.
    """

    def _dependency(user: ReviewerUser = Depends(get_current_user)) -> ReviewerUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="not permitted for this role")
        return user

    return _dependency
