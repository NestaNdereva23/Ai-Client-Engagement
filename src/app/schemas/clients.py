"""Response shapes for the client segment console endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ClientSummaryOut(BaseModel):
    """One client's buckets: no name, no code, no raw figures."""

    client_id: int
    unit_fund_id: int
    archetype: str | None
    recency_bucket: str | None
    value_tier: str | None
    rhythm_band: str | None
    message_angle: str | None
    priority_tier: str | None


class SegmentBucketOut(BaseModel):
    key: str | None
    count: int


class SegmentDistributionOut(BaseModel):
    by_archetype: list[SegmentBucketOut]
    by_value_tier: list[SegmentBucketOut]
    by_message_angle: list[SegmentBucketOut]
