"""Interim protection for the integration plane: one shared secret header.

A stopgap ahead of M8A.7's real scoped API keys or OAuth client-credentials.
Fails closed: with no key configured, every integration request is refused
rather than silently let through, since these endpoints write real PII
(contact channels) and compliance data (suppressions) with no other gate in
front of them yet.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.config import get_settings

INTEGRATION_KEY_HEADER = "X-Integration-Key"


def require_integration_key(x_integration_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.integration_api_key:
        raise HTTPException(status_code=503, detail="integration API key is not configured")
    if not x_integration_key or not secrets.compare_digest(
        x_integration_key, settings.integration_api_key
    ):
        raise HTTPException(status_code=401, detail="invalid or missing integration key")
