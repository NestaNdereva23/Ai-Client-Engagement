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


class ReviewActionOut(BaseModel):
    """One recorded decision, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    review_action_id: int
    reviewer_id: str
    outcome: str
    edited_content: dict | None
    reason: str | None
    created_at: datetime


class OutreachMessageDetail(OutreachMessageSummary):
    """A message with both content versions and its full decision history."""

    ai_draft_content: dict
    personalized_content: dict | None
    updated_at: datetime
    history: list[ReviewActionOut]
