from __future__ import annotations

from pydantic import BaseModel


class TestSmsSendRequest(BaseModel):
    to: str
    body: str


class TestSmsSendOut(BaseModel):
    sent: bool
    recipient: str
    parts: int
    provider_status: str | None = None
    reason: str = ""
