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
    # {skip_reason: count of client funds left out for it}. None on a
    # proposal written before this was tracked; an empty dict means every
    # client fund qualified.
    skip_reason_counts: dict[str, int] | None
    status: str
    permission_applied: str
    created_at: datetime
    decided_at: datetime | None
    card_title: str
    screen_label: str
    why_now: str
    suggested_owner: str | None


class AgentProposalClientOut(BaseModel):
    """One client the proposal considered, in or out, and why."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    included: bool
    skip_reason: str | None
    variant: str | None


class AgentProposalDetailOut(AgentProposalSummaryOut):
    """One proposal with its evidence, reason, and the full client breakdown."""

    group_definition: dict | None
    evidence: str
    reason: str
    angle: str | None
    content_mix: str | None
    campaign_id: int | None
    decided_by: str | None


class ProposalDecisionRequest(BaseModel):
    """A person's approve or reject call on one proposal."""

    decision: ProposalDecision
    reason: str = Field(min_length=1)


class ProposalStopRequest(BaseModel):
    """A person's call to stop a proposal that has drafted but not sent."""

    reason: str = Field(min_length=1)


class ProposalDecisionResultOut(BaseModel):
    """The proposal's state right after a decision is recorded."""

    model_config = ConfigDict(from_attributes=True)

    proposal_id: int
    status: str
    decided_by: str | None
    decided_at: datetime | None


class ProposalVersionSideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    variant: str
    angle: str
    guide_mix: str | None
    guide_label: str
    clients: int
    measured: int
    replied: int
    opted_out: int
    edited: int
    deposited: int
    money_in_kes: float
    reply_percent: float | None
    opt_out_percent: float | None
    edit_percent: float | None
    deposit_percent: float | None


class ProposalVersionsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    proposal_id: int
    action_code: str
    group_name: str
    status: str
    differs_in: Literal["angle", "guide"]
    window_days: int
    min_group_size: int
    enough_to_compare: bool
    sides: list[ProposalVersionSideOut]
