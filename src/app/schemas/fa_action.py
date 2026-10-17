from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

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


class BriefHeadlineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    lead: str
    text: str
    count: int
    amount_kes: float | None


class BriefCalendarItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_id: int
    kind: str
    label: str
    time: str | None


class BriefClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_code: str | None
    balance_kes: float | None
    fund_at_risk_kes: float | None
    reason: str


class BriefGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    title: str
    clients: int
    could_leave_kes: float
    share: float
    what_is_happening: str
    what_to_do: str
    worth_knowing: str
    sharp_fact: str | None
    examples: list[BriefClientOut]
    warning_signs: str | None = None


class DailyBriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fa_id: str
    advisor_name: str
    as_of: date
    assigned_clients: int
    worklist_clients: int
    headlines: list[BriefHeadlineOut]
    calendar: list[BriefCalendarItemOut]
    groups: list[BriefGroupOut]
    rest_clients: int
    rest_could_leave_kes: float
