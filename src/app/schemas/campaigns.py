"""Request and response shapes for the campaign console endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, model_validator


class CampaignSummaryOut(BaseModel):
    """One campaign's enrollment counts, including rows suppressed as a duplicate person."""

    campaign_id: int
    total_enrolled: int
    primary_count: int
    suppressed_count: int


class CampaignListItemOut(BaseModel):
    """One row of the campaign table: the campaign's own fields plus its enrollment counts."""

    campaign_id: int
    name: str
    campaign_type: str
    status: str
    cohort_definition: dict | None
    start_date: date | None
    end_date: date | None
    created_at: datetime
    total_enrolled: int
    primary_count: int
    suppressed_count: int


class CohortFilter(BaseModel):
    """The same allow-listed bucket filters GET /clients accepts.

    Stored as-is on the new campaign's cohort_definition, so membership can
    be re-derived later, and used once, at creation time, to resolve the
    client_ids actually enrolled.
    """

    fund_id: int | None = None
    value_band: str | None = None
    recency_band: str | None = None
    purchase_depth: str | None = None
    message_angle: str | None = None

    @model_validator(mode="after")
    def _at_least_one_filter(self) -> CohortFilter:
        fields = (
            self.fund_id,
            self.value_band,
            self.recency_band,
            self.purchase_depth,
            self.message_angle,
        )
        if not any(fields):
            raise ValueError(
                "cohort must set at least one filter, or it would enroll the entire book"
            )
        return self


class CampaignCreateRequest(BaseModel):
    name: str
    campaign_type: str = "dormant_reengagement"
    cohort: CohortFilter
    start_date: date | None = None
    end_date: date | None = None


class CampaignCreateOut(BaseModel):
    """A new campaign, with the cohort size resolved and enrolled at creation time."""

    campaign_id: int
    name: str
    campaign_type: str
    status: str
    cohort_definition: dict | None
    start_date: date | None
    end_date: date | None
    created_at: datetime
    enrolled_count: int


class CampaignStepCreateRequest(BaseModel):
    """One step to append to a campaign's send sequence.

    step_no is assigned server-side (one past whatever already exists), so
    building a sequence is calling this once per step in order rather than
    naming a position.
    """

    offset_days: int
    message_angle: str
    template_ref: str | None = None


class CampaignStepOut(BaseModel):
    step_id: int
    campaign_id: int
    step_no: int
    offset_days: int
    message_angle: str
    template_ref: str | None


class TouchOutcomeOut(BaseModel):
    """What happened to one due enrollment during a generation run."""

    enrollment_id: int
    generated: bool
    reason: str | None
    touch_id: int | None
