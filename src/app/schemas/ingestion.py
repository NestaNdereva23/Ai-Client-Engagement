"""Request and response shapes for the ingestion console endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TriggerIngestionRequest(BaseModel):
    """Start a fresh pull, or resume one that stopped."""

    endpoint: str = "inactive-clients"
    run_id: str | None = None
    max_pages: int = 1000


class IngestionRunAccepted(BaseModel):
    """The run has been queued; poll GET /ingestion/runs/{run_id} for progress."""

    run_id: str
    state: str = "running"


class IngestionRunOut(BaseModel):
    """One run's progress and counters."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    endpoint: str
    state: str
    started_at: datetime
    finished_at: datetime | None
    records_seen: int
    records_written: int
    records_rejected: int
    shortfall: int
