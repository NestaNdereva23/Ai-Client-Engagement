"""Response shapes for the client segment console endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ClientSummaryOut(BaseModel):
    """One client's buckets: no name, no code, no raw figures.

    call_brief is the one exception, only ever populated on the
    single-client detail read, never the list.
    """

    client_id: int
    unit_fund_id: int
    recency_band: str | None
    value_band: str | None
    cadence_band: str | None
    hold_band: str | None
    message_angle: str | None
    priority_tier: str | None
    call_brief: str | None = None


class SegmentBucketOut(BaseModel):
    key: str | None
    count: int


class SegmentDistributionOut(BaseModel):
    by_purchase_depth: list[SegmentBucketOut]
    by_value_band: list[SegmentBucketOut]
    by_message_angle: list[SegmentBucketOut]
    # A contact over three years stale never blocks a send; this is visibility
    # into the ramp a batch should ease into, not a count of anything held.
    stale_contact_count: int
