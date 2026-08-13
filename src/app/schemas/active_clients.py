"""Request and response shapes for the active-client endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

InteractionType = Literal["call_logged", "snoozed", "dismissed"]


class InteractionCreate(BaseModel):
    """One FA action logged against an active-client digest line."""

    type: InteractionType
    note: str | None = None


class InteractionOut(BaseModel):
    """One logged interaction, as stored."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    unit_fund_id: int
    type: str
    note: str | None
    reviewer_id: str
    created_at: datetime


class ActiveClientIdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_code: str | None
    fund_name: str


class ActiveClientBandsOut(BaseModel):
    """The current-state bands and score client_risk_features holds for
    this client-fund. Every field is null when no nightly run has scored
    it yet, not a 404 -- see ActiveClientProfileOut.
    """

    model_config = ConfigDict(from_attributes=True)

    recency_band: str | None
    balance_tier: str | None
    value_tier: str | None
    credible_rhythm: bool
    risk_score: int | None
    risk_band: str | None
    risk_reasons: str | None
    route: str | None
    aum_at_risk: float | None


class ActiveClientRiskHistoryEntryOut(BaseModel):
    """One risk_snapshot row: this client-fund's numbers as of one nightly run."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    risk_score: int
    risk_band: str
    risk_reasons: str
    route: str | None
    created_at: datetime


class ActiveClientComplaintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opened_at: date
    closed_at: date | None
    status: str
    category: str
    channel: str


class ActiveClientProfileOut(BaseModel):
    """Everything this codebase holds about one active-client-fund
    relationship, short of a name -- the active-book counterpart of
    ClientProfileOut (app.schemas.clients).
    """

    identity: ActiveClientIdentityOut
    bands: ActiveClientBandsOut
    risk_history: list[ActiveClientRiskHistoryEntryOut]
    complaints: list[ActiveClientComplaintOut]
    interactions: list[InteractionOut]
