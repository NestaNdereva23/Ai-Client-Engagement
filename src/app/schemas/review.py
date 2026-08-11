"""Request and response shapes for the review queue API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

ReviewOutcome = Literal["approve", "edit_approve", "reject", "escalate", "hold"]


class DecideRequest(BaseModel):
    """One reviewer decision on one message."""

    outcome: ReviewOutcome
    reviewer_id: str
    reason: str | None = None
    edited_content: dict | None = None

    @model_validator(mode="after")
    def _edit_approve_needs_content(self) -> DecideRequest:
        if self.outcome == "edit_approve" and not self.edited_content:
            raise ValueError("edit_approve requires edited_content")
        return self


class OutreachMessageSummary(BaseModel):
    """One queue row: enough to list and pick a message to open."""

    model_config = ConfigDict(from_attributes=True)

    message_id: str
    campaign_id: int
    client_id: int
    channel: str
    status: str
    created_at: datetime
    # Set when this message came from instantiating an approved
    # message_template rather than being drafted individually; lets the
    # review UI show it was already reviewed once at the template level.
    template_id: str | None = None


class ReviewActionOut(BaseModel):
    """One recorded decision, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    review_action_id: int
    reviewer_id: str
    outcome: str
    edited_content: dict | None
    message_angle: str | None
    priority_tier: str | None
    edit_diff: dict | None
    reason: str | None
    created_at: datetime


class OutreachMessageDetail(OutreachMessageSummary):
    """A message with both content versions and its full decision history."""

    ai_draft_content: dict
    personalized_content: dict | None
    # Set only for a tier whose contract adds a secondary call_brief
    # channel (today, T1); carries no PII (agents.email_agent.render_call_brief).
    call_brief: str | None = None
    updated_at: datetime
    history: list[ReviewActionOut]
