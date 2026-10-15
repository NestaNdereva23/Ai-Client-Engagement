from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.client_tiers import HeldOutCountOut


class FaActionClientOut(BaseModel):
    client_id: int
    client_code: str | None
    funds_held: int
    total_balance_kes: float | None
    owner_matched_by_email: bool


class FaActionGroupOut(BaseModel):
    tier: str
    tier_label: str
    segment: str
    segment_label: str
    segment_goal: str
    segment_definition: str
    tier_goal: str
    tier_action: str
    success_measure: str
    clients: int
    balance_kes: float
    fund_at_risk_kes: float
    items: list[FaActionClientOut]
    next_cursor: str | None = None


class FaActionHeldOutOut(HeldOutCountOut):
    balance_kes: float


class FaActionBriefingOut(BaseModel):
    fa_id: str
    text: str
    mode: str = "deterministic"


class FaActionBookOut(BaseModel):
    route: str
    fa_id: str
    fa_name: str | None
    assigned_clients: int
    computed_at: datetime | None
    groups: list[FaActionGroupOut]
    held_out: list[FaActionHeldOutOut]
    not_yet_tiered: int
    not_yet_tiered_balance_kes: float
