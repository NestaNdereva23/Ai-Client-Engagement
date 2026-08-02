"""Response shapes for the audit trail and trace console endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: int
    entity_type: str
    entity_id: str | None
    action: str
    actor_id: str | None
    run_id: str | None
    trace_id: str | None
    detail: dict | None
    created_at: datetime


class TraceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    trace_id: str
    trace_url: str | None
    created_at: datetime
