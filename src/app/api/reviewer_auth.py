"""Interim protection for the endpoints that re-attach a client's real name
or record a review decision.

GET /briefing/{client_id}/{unit_fund_id} also re-attaches a real name but
deliberately does not use this dependency -- see api/routers/briefing.py for
why. Don't assume every real-name read in this codebase is gated by it.

Nothing in this codebase authenticates a human reviewer with a real login
yet -- no session, no role. The gate is Ticketing's own login: a caller
sends the short-lived JWT Ticketing issues to its logged-in user as
Authorization: Bearer <token>, signed with a secret shared out of band
(AI_OUTREACH_JWT_SECRET here, matching Ticketing's env var of the same
name). This resolves the token's email claim to the reviewer_id an audited
action is recorded under, so it names the Ticketing user who actually made
the call, not whatever string a request body claims. With no secret
configured, every endpoint behind this refuses every request rather than
run unprotected.

This is a minimum acceptable gate, not the actual decision. Do not set
AI_OUTREACH_JWT_SECRET in any environment holding real client data until
real session/role auth exists.
"""

from __future__ import annotations

import jwt
from fastapi import Header, HTTPException

from app.config import get_settings

BEARER_PREFIX = "Bearer "


def get_current_reviewer_id(authorization: str | None = Header(default=None)) -> str:
    """Resolve Authorization: Bearer <jwt> to the reviewer_id it belongs to.

    503 with no secret configured, 401 for a missing, malformed, expired,
    or badly signed token, or one with no email claim.
    """
    secret = get_settings().ai_outreach_jwt_secret
    if not secret:
        raise HTTPException(status_code=503, detail="reviewer auth is not configured")
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    token = authorization[len(BEARER_PREFIX) :]
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token") from None

    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return email
