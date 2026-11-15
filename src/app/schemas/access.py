from __future__ import annotations

from pydantic import BaseModel


class AccessOut(BaseModel):
    email: str
    role: str
    fa_id: str | None
    viewing_as: str | None
    ai_assistant: bool


class AdvisorOut(BaseModel):
    email: str
    name: str
    fa_id: str | None


class ViewAsRequest(BaseModel):
    email: str
