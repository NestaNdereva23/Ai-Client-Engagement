from __future__ import annotations

from fastapi import Header

from app.api.reviewer_auth import decode_bearer_token


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    decode_bearer_token(authorization)
