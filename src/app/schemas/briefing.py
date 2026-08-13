"""Request and response shapes for the briefing endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BriefingOut(BaseModel):
    """One client-fund's rendered briefing page."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_name: str | None
    text: str
    basis: list[str]
