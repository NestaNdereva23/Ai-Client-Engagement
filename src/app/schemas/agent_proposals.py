"""Request and response shapes for the agent proposals API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProposalDecision = Literal["approve", "reject"]


class AgentProposalSummaryOut(BaseModel):
    """One proposal row: counts and money, never a client name."""

    model_config = ConfigDict(from_attributes=True)

    proposal_id: int
    action_code: str
    group_name: str
    client_count: int
    included_count: int | None
    money_total_kes: float | None
    status: str
    permission_applied: str
    created_at: datetime
    decided_at: datetime | None


class AgentProposalClientOut(BaseModel):
    """One client the proposal considered, in or out, and why."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    included: bool
    skip_reason: str | None


class AgentProposalDetailOut(AgentProposalSummaryOut):
    """One proposal with its evidence, reason, and the full client breakdown."""

    group_definition: dict | None
    evidence: str
    reason: str
    angle: str | None
    content_mix: str | None
    campaign_id: int | None
    decided_by: str | None
    clients: list[AgentProposalClientOut]


class ProposalDecisionRequest(BaseModel):
    """A person's approve or reject call on one proposal."""

    decision: ProposalDecision
    reason: str = Field(min_length=1)


class ProposalDecisionResultOut(BaseModel):
    """The proposal's state right after a decision is recorded."""

    model_config = ConfigDict(from_attributes=True)

    proposal_id: int
    status: str
    decided_by: str | None
    decided_at: datetime | None
