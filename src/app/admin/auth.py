"""Login gate for the admin UI: one shared username/password from settings.

A stopgap, the same shape as the integration plane's shared secret: no user
table yet, one admin account, session cookie signed by admin_secret_key. An
unset username or password refuses every login rather than run open.
"""

from __future__ import annotations

import hmac

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import get_settings

_SESSION_KEY = "admin_authenticated"


class AdminAuth(AuthenticationBackend):
    """Basic-auth-style login backed by a single configured admin account."""

    async def login(self, request: Request) -> bool:
        settings = get_settings()
        if not settings.admin_username or not settings.admin_password:
            return False

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))

        valid = hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
            password, settings.admin_password
        )
        if not valid:
            return False

        request.session[_SESSION_KEY] = True
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> RedirectResponse | bool:
        if request.session.get(_SESSION_KEY):
            return True
        return RedirectResponse(request.url_for("admin:login"), status_code=302)
