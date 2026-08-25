from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.api.reviewer_auth import bearer_scheme, decode_bearer_token


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    decode_bearer_token(credentials)
