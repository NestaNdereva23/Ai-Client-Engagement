"""Request and response shapes for the briefing endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BriefingOut(BaseModel):
    """One client-fund's rendered briefing page.

    mode is "deterministic" for GET /briefing/{id}/{fund}, and either
    "narrative" or "deterministic_fallback" for the AI-narrated route
    (GET /briefing/{id}/{fund}/narrative) -- the caller always knows which
    text it actually received, never just that a request succeeded.
    """

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_name: str | None
    text: str
    basis: list[str]
    mode: str = "deterministic"
