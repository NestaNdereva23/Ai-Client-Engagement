"""Response shape for the data-quality console endpoint."""

from __future__ import annotations

from pydantic import BaseModel


class RejectReasonCount(BaseModel):
    reason: str
    count: int


class DataQualityOut(BaseModel):
    run_id: str
    records_seen: int
    records_written: int
    records_rejected: int
    shortfall: int
    reject_reasons: list[RejectReasonCount]
