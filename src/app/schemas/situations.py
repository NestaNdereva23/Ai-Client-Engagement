from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SituationDeltaOut(BaseModel):
    situation_code: str
    run_id: str | None
    run_at: datetime | None
    newly_active: int
    newly_resolved: int
    resolved_by_second_deposit: int
    resolved_by_window_elapsed: int
    resolved_by_both: int
    persisting: int
