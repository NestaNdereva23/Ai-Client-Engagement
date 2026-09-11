"""Request and response shapes for the agent insights API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InsightDecision = Literal["accept", "dismiss"]


class AgentInsightSummaryOut(BaseModel):
    """One finding as it appears in the list: what it is and how big it is."""

    model_config = ConfigDict(from_attributes=True)

    insight_id: int
    run_id: int | None
    kind: str
    title: str
    group_name: str
    client_count: int
    money_total_kes: float | None
    confidence: str
    suggestion: str
    state: str
    created_at: datetime


class AgentInsightFactOut(BaseModel):
    """One counted number, with the filter and table it came from."""

    model_config = ConfigDict(from_attributes=True)

    fact_id: int
    fact_text: str
    fact_value: str
    source_filter: dict
    source_table: str


class AgentInsightDetailOut(AgentInsightSummaryOut):
    """One finding with its facts kept apart from what the model reads
    into them, so a reader never has to guess which is which.
    """

    group_definition: dict | None
    confidence_reason: str
    avoid_saying: str | None
    why_now: str
    dismissed_reason: str | None
    decided_by: str | None
    decided_at: datetime | None
    listed_client_count: int
    facts: list[AgentInsightFactOut]


class InsightKindCountsOut(BaseModel):
    """How many findings of each kind the current filters return."""

    counts_by_kind: dict[str, int]
    total_count: int


class InsightDecisionRequest(BaseModel):
    """A person's accept or dismiss call on one finding."""

    decision: InsightDecision
    reason: str = Field(min_length=1)


class InsightDecisionResultOut(BaseModel):
    """The finding's state right after a decision is recorded.

    Accepting a finding also starts the run that decides how to answer it,
    and action_run_id is that run. It is null for a dismissal, and null for
    an acceptance whose run could not be started.
    """

    model_config = ConfigDict(from_attributes=True)

    insight_id: int
    state: str
    dismissed_reason: str | None
    action_run_id: int | None = None
    decided_by: str | None
    decided_at: datetime | None


class FactRecountOut(BaseModel):
    """What a fact's filter counts today, beside the value that was stored."""

    fact_id: int
    as_of: date
    stored_value: str
    can_recount: bool
    group_name: str | None
    client_count: int | None
    fund_count: int | None
    money_total_kes: float | None
    reason: str | None
