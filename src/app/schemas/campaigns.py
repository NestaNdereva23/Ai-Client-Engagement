"""Response shapes for the campaign console endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class CampaignSummaryOut(BaseModel):
    """One campaign's enrollment counts, including rows suppressed as a duplicate person."""

    campaign_id: int
    total_enrolled: int
    primary_count: int
    suppressed_count: int
