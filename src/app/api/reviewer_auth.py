from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def decode_bearer_token(credentials: HTTPAuthorizationCredentials | None) -> dict:
    secret = get_settings().ai_outreach_jwt_secret
    if not secret:
        raise HTTPException(status_code=503, detail="reviewer auth is not configured")
    if credentials is None:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    try:
        return jwt.decode(credentials.credentials, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token") from None


def get_current_reviewer_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    payload = decode_bearer_token(credentials)
    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return email
