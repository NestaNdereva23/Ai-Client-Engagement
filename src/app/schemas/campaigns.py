"""Request and response shapes for the campaign console endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, model_validator


class OutreachBucketOut(BaseModel):
    """One count bucket, shared by every book-wide outreach-analytics breakdown."""

    key: str | None
    count: int


class OutreachAnalyticsOut(BaseModel):
    """Book-wide outreach analytics across every campaign: the enrollment
    funnel, cohort composition, drafting/review throughput, and how contact
    ends -- the dormant-outreach counterpart to GET /risk/analytics.

    total_enrolled/primary_count/suppressed_count sum the same split
    CampaignSummaryOut reports per campaign, across every campaign at once.
    reengaged_count is how many primary enrollments carry
    stopped_reengaged, the win-back funnel's actual success state;
    reengagement_rate divides that by primary_count, since a suppressed row
    never sends and can never reach it.
    """

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
    """One calendar day's book-wide send and response activity."""

    day: date
    touches_sent: int
    replies: int
    bounces: int


class OutreachTrendOut(BaseModel):
    """The last N calendar days' book-wide send and response activity,
    oldest first: the trend counterpart to the point-in-time /analytics
    snapshot above.
    """

    points: list[OutreachTrendPointOut]


class CampaignSummaryOut(BaseModel):
    """One campaign's enrollment counts, including rows suppressed as a duplicate person."""

    campaign_id: int
    total_enrolled: int
    primary_count: int
    suppressed_count: int


class CampaignValueOut(BaseModel):
    """What one campaign's cohort was worth, for ROI reporting.

    estimated_value sums total_purchase_amount (KES) across primary
    enrollment rows only, the same scope campaign_summary's primary_count
    uses. valued_count is how many of those rows actually joined to a
    Clients row, which should equal primary_count from GET
    .../summary unless a client record is missing.
    """

    campaign_id: int
    valued_count: int
    estimated_value: float


class CampaignReadinessOut(BaseModel):
    """Per-status counts for one campaign's templates and messages, so
    "is this campaign fully drafted and approved" is one read instead of
    paging GET /reviews and GET /templates across every status and
    tallying client-side.

    Keys are the status values each table's own check constraint allows
    (pending_review, approved, rejected, escalated, held); a status with
    no rows in it is simply absent rather than listed as zero.
    """

    campaign_id: int
    templates: dict[str, int]
    messages: dict[str, int]


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
    """One campaign's own fields, with no enrollment counts attached.

    The counts live at GET /campaigns/{campaign_id}/summary; this is the
    plain read a page needs when it only has a campaign_id in the URL and
    no already-fetched list row to scavenge fields from.
    """

    campaign_id: int
    name: str
    campaign_type: str
    status: str
    cohort_definition: dict | None
    start_date: date | None
    end_date: date | None
    created_at: datetime


class EnrollmentOut(BaseModel):
    """One row of a campaign's enrollment roster.

    Distinct from a review-queue row: an enrolled client shows up here
    whether or not a message has ever been drafted for them.
    """

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
