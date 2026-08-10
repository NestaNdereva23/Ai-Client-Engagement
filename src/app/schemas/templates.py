"""Request and response shapes for the template review and instantiation API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.review import OutreachMessageSummary, ReviewOutcome


class DecideTemplateRequest(BaseModel):
    """One reviewer decision on one template."""

    outcome: ReviewOutcome
    reviewer_id: str
    reason: str | None = None
    edited_content: dict | None = None

    @model_validator(mode="after")
    def _edit_approve_needs_content(self) -> DecideTemplateRequest:
        if self.outcome == "edit_approve" and not self.edited_content:
            raise ValueError("edit_approve requires edited_content")
        return self


class MessageTemplateSummary(BaseModel):
    """One queue row: enough to list and pick a template to open."""

    model_config = ConfigDict(from_attributes=True)

    template_id: str
    campaign_id: int
    status: str
    profile_key: dict
    created_at: datetime


class TemplateReviewActionOut(BaseModel):
    """One recorded template decision, as returned by the API."""

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


class MessageTemplateDetail(MessageTemplateSummary):
    """A template with its draft and full decision history."""

    generation_run_id: str
    ai_draft_content: dict
    updated_at: datetime
    history: list[TemplateReviewActionOut]


class DraftTemplatesResult(BaseModel):
    """How many templates one drafting call produced, and which."""

    drafted_count: int
    templates: list[MessageTemplateSummary]


class InstantiateTemplateResult(BaseModel):
    """How many messages one instantiation call produced, and which."""

    instantiated_count: int
    messages: list[OutreachMessageSummary]
