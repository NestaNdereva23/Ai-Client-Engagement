from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ClientTierTagOut(BaseModel):
    segment: str | None = None
    segment_label: str | None = None
    tier: str | None = None
    tier_label: str | None = None
    tier_goal: str | None = None
    tier_action: str | None = None


class ClientTierDetailOut(ClientTierTagOut):
    success_measure: str | None = None
    purchased_recently: bool | None = None
    real_withdrawer: bool | None = None
    computed_at: datetime | None = None


class SegmentCountOut(BaseModel):
    segment: str
    segment_label: str
    segment_goal: str
    clients: int


class TierCountOut(BaseModel):
    tier: str
    tier_label: str
    segment: str
    segment_label: str
    tier_goal: str
    tier_action: str
    clients: int


class HeldOutCountOut(BaseModel):
    tier: str
    tier_label: str
    clients: int


class TierSummaryOut(BaseModel):
    active_clients: int
    inactive_clients: int
    segments: list[SegmentCountOut]
    tiers: list[TierCountOut]
    held_out: list[HeldOutCountOut]
    active_clients_not_yet_tiered: int
    computed_at: datetime | None
