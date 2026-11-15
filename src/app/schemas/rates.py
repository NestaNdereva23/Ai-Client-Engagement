from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class VerifiedRateIn(BaseModel):
    rate_value: float
    source: str


class VerifiedRateOut(BaseModel):
    product: str
    rate_value: float
    source: str
    recorded_by: str
    as_of: datetime
    is_fresh: bool
