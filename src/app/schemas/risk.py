"""Request and response shapes for the risk endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RiskCoverageOut(BaseModel):
    """The active book's size vs. how many of them the last completed
    nightly run actually scored.
    """

    model_config = ConfigDict(from_attributes=True)

    book_size: int
    scored_count: int
    as_of: datetime | None


class DustCleanupLineOut(BaseModel):
    """One client-fund currently routed to dust_cleanup."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    balance_tier: str | None
    risk_score: int
    risk_band: str
    risk_reasons: str
    aum_at_risk: float
