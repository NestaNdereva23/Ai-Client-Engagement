"""Response shapes for reading back what happened during a run."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AgentEventOut(BaseModel):
    """One thing that happened, and where it sits in the order."""

    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    kind: str
    detail: dict
    created_at: datetime


class RunProgressOut(BaseModel):
    """Where the run has got to, counted from its own events."""

    model_config = ConfigDict(from_attributes=True)

    groups_total: int | None
    groups_looked_at: int
    insights_found: int
    waiting_on_a_person: int
    doing_now: str | None
    finished: bool
    outcome: str | None


class RunActivityOut(BaseModel):
    """The next stretch of a run's events, and where it has got to.

    last_ordinal is what a caller sends back as after next time. It is the
    end of the whole run, not the end of this stretch, so a caller can tell
    there is more waiting for it.
    """

    run_id: int
    state: str
    events: list[AgentEventOut]
    last_ordinal: int
    progress: RunProgressOut
