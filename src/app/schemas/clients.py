"""Response shapes for the client segment console endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.pagination import Page
from app.schemas.rules import AngleStatusOut


class ClientSummaryOut(BaseModel):
    client_id: int
    unit_fund_id: int
    recency_band: str | None
    value_band: str | None
    cadence_band: str | None
    hold_band: str | None
    purchase_depth: str | None
    message_angle: str | None
    priority_tier: str | None
    call_brief: str | None = None


class ClientIdentityOut(BaseModel):
    client_id: int
    client_code: str | None
    unit_fund_id: int
    fund_name: str | None
    fund_type: str | None
    n_funds: int | None
    holds_other_funds: bool | None


class ClientBandsOut(BaseModel):
    """The behavioural bands describing the relationship this client is contacted on."""

    recency_band: str | None
    value_band: str | None
    cadence_band: str | None
    hold_band: str | None
    purchase_depth: str | None
    trend_band: str | None
    exit_reason: str | None
    own_rhythm_days: int | None


class ClientFlagsOut(BaseModel):
    in_wave: bool | None
    newly_dormant: bool | None
    has_depth: bool | None
    staged_exit: bool | None
    stale_contact: bool | None
    history_censored: bool | None
    purchases_censored: bool | None


class ClientActivityOut(BaseModel):
    last_activity_date: date | None
    days_since_last_activity: int | None
    observed_volume: int | None
    n_purchases_returned: int | None
    total_purchase_amount: float | None
    computed_at: str | None


class ClientRoutingOut(BaseModel):
    message_angle: str | None
    priority_tier: str | None
    urgency: str | None
    prompt_variant: str | None
    rule_name: str | None
    rule_version: int | None


class ClientEnrollmentOut(BaseModel):
    enrollment_id: int
    campaign_id: int
    status: str
    current_step: int
    next_due_at: datetime | None
    enrolled_at: datetime
    is_primary_contact_row: bool


class ClientTouchOut(BaseModel):
    """One generated or sent touch."""

    touch_id: int
    enrollment_id: int
    step_no: int
    message_id: str | None
    sent_at: datetime | None
    delivery_status: str | None
    created_at: datetime


class ClientOutreachMessageOut(BaseModel):
    message_id: str
    campaign_id: int
    template_id: str | None
    channel: str
    status: str
    created_at: datetime
    updated_at: datetime


class ClientContactEventOut(BaseModel):
    id: int
    type: str
    occurred_at: datetime
    created_at: datetime


class ClientSuppressionOut(BaseModel):
    is_suppressed: bool
    reason: str | None
    source: str | None
    created_at: datetime | None


class ClientProfileOut(BaseModel):
    identity: ClientIdentityOut
    bands: ClientBandsOut
    flags: ClientFlagsOut
    activity: ClientActivityOut
    routing: ClientRoutingOut
    enrollments: list[ClientEnrollmentOut]
    touch_log: list[ClientTouchOut]
    outreach_messages: list[ClientOutreachMessageOut]
    contact_events: list[ClientContactEventOut]
    suppression: ClientSuppressionOut
    call_brief: str | None


class ClientNameOut(BaseModel):
    client_id: int
    client_name: str | None


class SegmentBucketOut(BaseModel):
    key: str | None
    count: int


class ValueRecencyBucketOut(BaseModel):
    """One cell of the value-band x recency-band cross-tab."""

    value_band: str | None
    recency_band: str | None
    count: int


class SegmentDistributionOut(BaseModel):
    by_purchase_depth: list[SegmentBucketOut]
    by_value_band: list[SegmentBucketOut]
    by_cadence_band: list[SegmentBucketOut]
    by_message_angle: list[SegmentBucketOut]
    by_value_and_recency: list[ValueRecencyBucketOut]
    # A contact over three years stale never blocks a send; this is visibility
    # into the ramp a batch should ease into, not a count of anything held.
    stale_contact_count: int
    history_censored_count: int
    purchases_censored_count: int
    unknown_recency_count: int


class ClientBookSummaryOut(BaseModel):
    total_clients: int
    fund_count: int


class EnrollmentSummaryOut(BaseModel):
    enrolled_count: int
    excluded_count: int


class SuppressionReasonCountOut(BaseModel):
    reason: str
    count: int


class SuppressionSummaryOut(BaseModel):
    suppressed_count: int
    by_reason: list[SuppressionReasonCountOut]


class ReengagementSummaryOut(BaseModel):
    primary_count: int
    reengaged_count: int
    reengagement_rate: float


class ClientsOverviewOut(BaseModel):
    book: ClientBookSummaryOut
    segments: SegmentDistributionOut
    enrollment: EnrollmentSummaryOut
    suppression: SuppressionSummaryOut
    reengagement: ReengagementSummaryOut
    angles: list[AngleStatusOut]
    records_rejected: int | None
    roster: Page[ClientSummaryOut]
