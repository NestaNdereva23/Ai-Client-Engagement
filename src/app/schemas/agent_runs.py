"""Request and response shapes for the agent runs API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class AgentRunOut(BaseModel):
    """One pass of the agent loop: when it ran, what triggered it, and what
    it did.
    """

    model_config = ConfigDict(from_attributes=True)

    run_id: int
    state: str
    trigger: str
    risk_run_id: str | None
    started_at: datetime
    finished_at: datetime | None
    plan_text: str | None
    summary: str | None
    cost_kes: float | None
    failure_reason: str | None


class AgentRunStartRequest(BaseModel):
    """The one thing a caller may choose when starting a run: which date to
    run it as of. Defaults to today.
    """

    as_of: date | None = None
