from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

PatternReviewStatus = Literal["worth_a_group_filter", "set_aside"]


class ObservedPatternOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pattern_id: int
    description: str
    features: dict[str, str]
    outcome: str
    compared_with: str
    direction: str
    window_days: int
    outcome_percent: float
    comparison_percent: float
    gap_points: float
    sent_count: int
    comparison_count: int
    status: str
    insight_id: int | None
    found_at: datetime
    last_seen_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None


class PatternStatusCountsOut(BaseModel):
    counts_by_status: dict[str, int]
    total_count: int


class PatternReviewRequest(BaseModel):
    status: PatternReviewStatus
