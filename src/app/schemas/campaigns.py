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
    newly_dormant: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_filter(self) -> CohortFilter:
        fields = (
            self.fund_id,
            self.value_band,
            self.recency_band,
            self.purchase_depth,
            self.message_angle,
            self.newly_dormant,
        )
        # is not None, not truthiness: newly_dormant=False is a real filter
        # (exclude the newly dormant), not an absent one.
        if not any(f is not None for f in fields):
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


class GenerationBatchOut(BaseModel):
    """One submission to the model provider's async batch endpoint."""

    generation_batch_id: str
    campaign_id: int
    provider: str
    provider_batch_id: str | None
    status: str
    requested_limit: int
    requested_count: int
    succeeded_count: int | None
    errored_count: int | None
    submitted_at: datetime | None
    ended_at: datetime | None
    ingested_at: datetime | None
    created_at: datetime


class BatchIngestOutcomeOut(BaseModel):
    """What happened to one client's request when its batch was ingested."""

    custom_id: str
    status: str
    reason: str | None


class BatchIngestResultOut(BaseModel):
    """The result of one ingest call: the batch's current state, plus
    whatever this call actually turned into a reviewable message.
    """

    batch: GenerationBatchOut
    outcomes: list[BatchIngestOutcomeOut]
