"""Request and response shapes for the digest endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DigestLineOut(BaseModel):
    """One client-fund's line, already ranked within its group."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    rank: int
    risk_score: int
    risk_band: str
    risk_reasons: str
    aum_at_risk: float
    score_delta: int | None
    route: str
    in_call_queue: bool
    complaint_caveat: bool


class DigestGroupOut(BaseModel):
    """Today's digest for one FA (or fund) group."""

    model_config = ConfigDict(from_attributes=True)

    digest_run_id: int
    risk_run_id: str
    generated_at: datetime
    group_key: str
    total_eligible: int
    overflow_count: int
    lines: list[DigestLineOut]
