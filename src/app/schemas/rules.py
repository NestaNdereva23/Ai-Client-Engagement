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

    recency_band: str | None = None
    value_band: str | None = None
    cadence_band: str | None = None
    hold_band: str | None = None
    purchase_depth: str | None = None
    trend_band: str | None = None
    exit_reason: str | None = None
    fund_type: str | None = None
    in_wave: bool | None = None
    has_depth: bool | None = None
    staged_exit: bool | None = None
    stale_contact: bool | None = None
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
