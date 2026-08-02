"""Request and response shapes for the business-rules console endpoints."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class RuleVersionOut(BaseModel):
    version: int
    valid_from: date
    valid_to: date | None
    rule_count: int
    is_active: bool


class RulePreviewRequest(BaseModel):
    """A feature tuple to dry-run through the active rule set."""

    archetype: str | None = None
    recency_bucket: str | None = None
    value_tier: str | None = None
    rhythm_band: str | None = None
    history_censored: bool | None = None
    purchases_censored: bool | None = None
    holds_other_funds: bool | None = None
    at: date | None = None


class RulePreviewOut(BaseModel):
    message_angle: str
    urgency: str
    priority_tier: str
    prompt_variant: str
    rule_id: int | None
    rule_name: str
    version: int | None
