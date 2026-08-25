from __future__ import annotations

import jwt
from fastapi import Header, HTTPException

from app.config import get_settings

BEARER_PREFIX = "Bearer "


def decode_bearer_token(authorization: str | None) -> dict:
    secret = get_settings().ai_outreach_jwt_secret
    if not secret:
        raise HTTPException(status_code=503, detail="reviewer auth is not configured")
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    token = authorization[len(BEARER_PREFIX) :]
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token") from None


def get_current_reviewer_id(authorization: str | None = Header(default=None)) -> str:
    payload = decode_bearer_token(authorization)
    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return email
