"""Request and response shapes for the campaign console endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    actual_sms_cost_kes: float
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
    problems: list[str] = Field(default_factory=list)


class CampaignListItemOut(BaseModel):
    """One row of the campaign table: the campaign's own fields plus its enrollment counts."""

    campaign_id: int
    name: str
    campaign_type: str
    status: str
    cohort_definition: dict | None
    default_channel: str
    is_test: bool
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
    default_channel: str
    is_test: bool
    start_date: date | None
    end_date: date | None
    created_at: datetime
    planning_mode: str
    agent_touch_policy: dict | None


DEFAULT_MIN_GAP_DAYS = 2
DEFAULT_MAX_GAP_DAYS = 10
DEFAULT_OBSERVATION_DAYS = 30

CampaignObjective = Literal["win_back_deposit", "top_up", "reactivate_never_funded"]


class CampaignPlanConfig(BaseModel):
    objective: CampaignObjective | None = None
    min_gap_days: int = Field(default=DEFAULT_MIN_GAP_DAYS, ge=0)
    max_gap_days: int = Field(default=DEFAULT_MAX_GAP_DAYS, ge=0)
    allowed_angles: list[str] = Field(default_factory=list)
    success_measure: Literal["deposit"] = "deposit"
    observation_days: int = Field(default=DEFAULT_OBSERVATION_DAYS, ge=1)

    @model_validator(mode="after")
    def _gaps_in_order(self) -> CampaignPlanConfig:
        if self.min_gap_days > self.max_gap_days:
            raise ValueError(
                f"min_gap_days {self.min_gap_days} may not exceed max_gap_days {self.max_gap_days}"
            )
        return self


class SentMessageOut(BaseModel):
    touch_id: int
    message_id: str | None
    client_id: int
    client_name: str | None = None
    channel: str
    sent_at: datetime
    delivery_status: str | None


class CitedFactOut(BaseModel):
    text: str
    url: str | None = None
    retrieved_at: str | None = None
    tool: str | None = None


class TouchPlanOut(BaseModel):
    enrollment_id: int
    client_id: int
    step_no: int
    prior_angle: str | None
    angle: str
    gap_days: int
    reason: str
    next_time_note: str | None = None
    cited_facts: list[CitedFactOut] | None


class ExplainPlanOut(BaseModel):
    run_id: int
    gap_days: int
    reason: str
    next_time_note: str | None = None
    cited_facts: list[CitedFactOut] | None = None


class ExplainDraftOut(BaseModel):
    message_id: str
    subject: str | None = None
    body: str | None = None
    status: str


class ExplainApprovalOut(BaseModel):
    outcome: str
    reviewer_id: str
    decided_at: datetime


class ExplainSendOut(BaseModel):
    touch_id: int
    sent_at: datetime | None = None
    delivery_status: str | None = None


class ExplainOutcomeOut(BaseModel):
    window_days: int
    opened: bool
    replied: bool
    bounced: bool
    deposit_seen: bool
    deposit_amount_kes: float
    moved_to_active: bool | None = None
    balance_up: bool | None = None
    measured_at: datetime


class ExplainTouchOut(BaseModel):
    enrollment_id: int
    client_id: int
    step_no: int
    angle: str | None = None
    stage: str
    plan: ExplainPlanOut | None = None
    draft: ExplainDraftOut | None = None
    approval: ExplainApprovalOut | None = None
    send: ExplainSendOut | None = None
    outcome: ExplainOutcomeOut | None = None


class ExplainResultsSeenOut(BaseModel):
    called_at: datetime
    results: dict


class ExplainRunOut(BaseModel):
    run_id: int
    started_at: datetime
    state: str
    results_seen: list[ExplainResultsSeenOut]


class CampaignExplainOut(BaseModel):
    touches: list[ExplainTouchOut]
    runs: list[ExplainRunOut]


class CampaignAgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: int
    kind: str
    campaign_id: int | None = None
    trigger: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    summary: str | None = None
    failure_reason: str | None = None


class AgentCampaignFactsOut(BaseModel):
    name: str
    status: str
    planning_mode: str
    is_test: bool
    objective: str | None = None
    success_measure: str
    observation_days: int
    min_gap_days: int
    max_gap_days: int
    cohort: dict | None = None
    enrolled: int
    touch_points: int
    allowed_angle_count: int | None = None
    live_angle_count: int


class AgentCheckOut(BaseModel):
    label: str
    ok: bool
    problems: list[str]


class RejectionOut(BaseModel):
    code: str
    slot: int | None = None
    message: str


class AgentRunRowOut(BaseModel):
    run_id: int
    started_at: datetime
    finished_at: datetime | None = None
    trigger: str
    state: str
    summary: str | None = None
    failure_reason: str | None = None
    cost_kes: float | None = None
    due: int | None = None
    planned: int
    left_out: dict[str, int]
    rejections: list[RejectionOut]


class PlannedTouchRowOut(BaseModel):
    client_id: int
    band: str | None = None
    step_no: int
    touch_points: int
    last_angle: str | None = None
    last_subject: str | None = None
    angle: str
    family: str | None = None
    suggested_subject: str | None = None
    gap_days: int
    reason: str
    next_time_note: str | None = None


class PlannedTouchesOut(BaseModel):
    total: int
    touches: list[PlannedTouchRowOut]


class ReviewProgressOut(BaseModel):
    samples: int
    approved: int
    waiting: int
    others_waiting: int
    ready_cohorts: list[str]


class ToolUsedOut(BaseModel):
    name: str
    calls: int


class BandCountOut(BaseModel):
    name: str
    count: int


class AgentGroupOut(BaseModel):
    tier: str
    campaign_band: str | None = None
    total: int
    bands: list[BandCountOut]


class LiveAgentCampaignsOut(BaseModel):
    live: int
    allowed: int


class CampaignAgentOverviewOut(BaseModel):
    campaign: AgentCampaignFactsOut
    checks: list[AgentCheckOut]
    latest_run: AgentRunRowOut | None = None
    recent_runs: list[AgentRunRowOut]
    plan_run_id: int | None = None
    planned: PlannedTouchesOut
    review: ReviewProgressOut
    tools_used: list[ToolUsedOut]
    cited_facts: list[CitedFactOut]
    results: dict
    group: AgentGroupOut | None = None
    live_agent_campaigns: LiveAgentCampaignsOut


class EnrollmentOut(BaseModel):
    enrollment_id: int
    campaign_id: int
    client_id: int
    client_name: str | None = None
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
    priority_tier: str | None = None
    high_value: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_filter(self) -> CohortFilter:
        fields = (
            self.fund_id,
            self.value_band,
            self.recency_band,
            self.purchase_depth,
            self.newly_dormant,
            self.priority_tier,
            self.high_value,
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
    priority_tier: str | None = None
    high_value: bool | None = None
    angles: list[str] = []


class CampaignStepCreateRequest(BaseModel):
    offset_days: int
    message_angle: str | None = None
    template_ref: str | None = None
    channel: str | None = None


class CampaignStepOut(BaseModel):
    step_id: int
    campaign_id: int
    step_no: int
    offset_days: int
    message_angle: str | None
    template_ref: str | None
    channel: str | None


class CampaignCreateRequest(BaseModel):
    name: str
    campaign_type: str = "dormant_reengagement"
    cohort: CohortFilter
    steps: list[CampaignStepCreateRequest] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    default_channel: str = "email"
    is_test: bool = False


class CampaignCreateOut(BaseModel):
    """A new campaign, with the cohort size resolved and enrolled at creation time."""

    campaign_id: int
    name: str
    campaign_type: str
    status: str
    cohort_definition: dict | None
    default_channel: str
    is_test: bool
    start_date: date | None
    end_date: date | None
    created_at: datetime
    enrolled_count: int
    steps: list[CampaignStepOut]


class CampaignDraftIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    cohort: CohortFilter
    planning_mode: Literal["fixed_steps", "agent"] = "agent"
    plan_config: CampaignPlanConfig
    touch_offsets: list[int] = Field(min_length=1, max_length=10)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _complete(self) -> CampaignDraftIn:
        if self.plan_config.objective is None:
            raise ValueError("a suggested campaign needs an objective")
        offsets = self.touch_offsets
        if offsets[0] < 0 or any(
            later <= earlier for earlier, later in zip(offsets, offsets[1:], strict=False)
        ):
            raise ValueError("touch_offsets must start at day 0 or later and rise with each touch")
        return self


class SuggestedCampaignOut(BaseModel):
    campaign_id: int
    run_id: int | None
    created_at: datetime
    draft: CampaignDraftIn
    waiting_now: int
    problems: list[str]


class SuggestionApprovedOut(BaseModel):
    campaign_id: int
    enrolled_count: int


class SuggestionDismissIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class WaitingCellOut(BaseModel):
    tier: str
    value_band: str
    count: int


class WaitingPoolOut(BaseModel):
    total: int
    by_tier: dict[str, int]
    by_angle: dict[str, int]
    by_value_band: dict[str, int]
    by_tier_and_value_band: list[WaitingCellOut]
    groups_too_small_to_show: int


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


class DeliveryOut(BaseModel):
    """One ready-to-send message handed to the Ticketing app to deliver."""

    channel: str
    to: str
    subject: str | None
    body: str
    cc: list[str] = Field(default_factory=list)


class CampaignDispatchOut(BaseModel):
    """The touches claimed for sending, and the messages Ticketing must deliver for them."""

    outcomes: list[TouchSendOutcomeOut]
    deliveries: list[DeliveryOut]


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
