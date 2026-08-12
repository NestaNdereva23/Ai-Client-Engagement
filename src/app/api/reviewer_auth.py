"""Interim protection for the one endpoint that re-attaches a client's real name.

Nothing in this codebase authenticates a human reviewer yet -- no session,
no role. Un-masking a name on an endpoint with no gate at all would be a
real data exposure, so until real session/role auth exists, the safe
default is the same one app.api.integration_auth already uses for the
integration plane: one shared secret header, fails closed. With no key
configured, the endpoint refuses every request rather than run unprotected.

This is a minimum acceptable gate, not the actual decision. Do not set
REVIEWER_API_KEY in any environment holding real client data until real
auth exists.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.config import get_settings

REVIEWER_KEY_HEADER = "X-Reviewer-Key"


def require_reviewer_key(x_reviewer_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.reviewer_api_key:
        raise HTTPException(status_code=503, detail="reviewer API key is not configured")
    if not x_reviewer_key or not secrets.compare_digest(x_reviewer_key, settings.reviewer_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing reviewer key")
