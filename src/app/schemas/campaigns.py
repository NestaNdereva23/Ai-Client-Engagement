"""Request and response shapes for the campaign console endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class OutreachBucketOut(BaseModel):
    key: str | None
    count: int


class OutreachAnalyticsOut(BaseModel):
    total_enrolled: int
    primary_count: int
    suppressed_count: int
    active_campaign_count: int
    by_enrollment_status: list[OutreachBucketOut]
    by_value_band: list[OutreachBucketOut]
    by_recency_band: list[OutreachBucketOut]
    by_priority_tier: list[OutreachBucketOut]
    by_message_angle: list[OutreachBucketOut]
    by_message_status: list[OutreachBucketOut]
    by_review_outcome: list[OutreachBucketOut]
    by_contact_event: list[OutreachBucketOut]
    reengaged_count: int
    reengagement_rate: float


class OutreachTrendPointOut(BaseModel):
    day: date
    touches_sent: int
    replies: int
    bounces: int


class OutreachTrendOut(BaseModel):
    points: list[OutreachTrendPointOut]


class CampaignSummaryOut(BaseModel):
    """One campaign's enrollment counts, including rows suppressed as a duplicate person."""

    campaign_id: int
    total_enrolled: int
    primary_count: int
    suppressed_count: int


class CampaignValueOut(BaseModel):
    campaign_id: int
    valued_count: int
    estimated_value: float


class GenerationCostScenarioOut(BaseModel):
    count_per_step: int
    cost_per_step_usd: float
    cost_per_step_kes: float
    total_cost_usd: float
    total_cost_kes: float


class GenerationCostOut(BaseModel):
    campaign_id: int
    model: str
    config_version: int
    rate_per_generation_usd: float
    rate_per_generation_kes: float
    step_count: int
    enrolled_clients: int
    estimated_templates: int
    single_generation: GenerationCostScenarioOut
    templates: GenerationCostScenarioOut
    as_of: datetime


class GenerationCostModelOut(BaseModel):
    """One model's current per-generation rate, for the model picker.

    label is a display name (e.g. "Claude Opus 5"); model is the id an
    estimate request's ?model= would use to select it.
    """

    model: str
    label: str
    config_version: int
    rate_per_generation_usd: float
    rate_per_generation_kes: float


class CampaignReadinessOut(BaseModel):
    """Per-status counts for one campaign's templates and messages, plus how much is unsent."""

    campaign_id: int
    templates: dict[str, int]
    messages: dict[str, int]
    sendable_now: int
    sent_count: int
    next_due_at: datetime | None = None


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


class CampaignDetailOut(BaseModel):
    campaign_id: int
    name: str
    campaign_type: str
    status: str
    cohort_definition: dict | None
    start_date: date | None
    end_date: date | None
    created_at: datetime


class EnrollmentOut(BaseModel):
    enrollment_id: int
    campaign_id: int
    client_id: int
    status: str
    current_step: int
    next_due_at: datetime | None
    priority_tier: str | None
    message_angle: str | None
    value_band: str | None
    recency_band: str | None


class CohortFilter(BaseModel):
    fund_id: int | None = None
    value_band: str | None = None
    recency_band: str | None = None
    purchase_depth: str | None = None
    newly_dormant: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_filter(self) -> CohortFilter:
        fields = (
            self.fund_id,
            self.value_band,
            self.recency_band,
            self.purchase_depth,
            self.newly_dormant,
        )
        if not any(f is not None for f in fields):
            raise ValueError(
                "cohort must set at least one filter, or it would enroll the entire book"
            )
        return self


class CohortPreviewOut(BaseModel):
    matched_count: int
    primary_count: int
    suppressed_count: int
    valued_count: int
    estimated_value: float


class CohortPreviewNarrowOut(BaseModel):
    matched_count: int
    estimated_value: float


class CohortPreviewAngleOut(BaseModel):
    message_angle: str
    matched_count: int
    estimated_value: float


class CohortPreviewBatchOut(BaseModel):
    narrow: CohortPreviewNarrowOut
    angles: list[CohortPreviewAngleOut]


class CohortPreviewBatchRequest(BaseModel):
    fund_id: int | None = None
    value_band: str | None = None
    recency_band: str | None = None
    purchase_depth: str | None = None
    newly_dormant: bool | None = None
    angles: list[str] = []


class CampaignStepCreateRequest(BaseModel):
    offset_days: int
    message_angle: str | None = None
    template_ref: str | None = None


class CampaignStepOut(BaseModel):
    step_id: int
    campaign_id: int
    step_no: int
    offset_days: int
    message_angle: str | None
    template_ref: str | None


class CampaignCreateRequest(BaseModel):
    name: str
    campaign_type: str = "dormant_reengagement"
    cohort: CohortFilter
    steps: list[CampaignStepCreateRequest] = Field(default_factory=list)
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
    steps: list[CampaignStepOut]


class TouchOutcomeOut(BaseModel):
    """What happened to one due enrollment during a generation run."""

    enrollment_id: int
    generated: bool
    reason: str | None
    touch_id: int | None


class TouchSendOutcomeOut(BaseModel):
    """What happened when one touch's approved message was handed to the sender."""

    touch_id: int
    enrollment_id: int
    sent: bool
    delivery_status: str | None
    reason: str | None


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
