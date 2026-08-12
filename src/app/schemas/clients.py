"""Response shapes for the client segment console endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class ClientSummaryOut(BaseModel):
    """One client's buckets: no name, no code, no raw figures.

    call_brief is the one exception, only ever populated on the
    single-client detail read, never the list.
    """

    client_id: int
    unit_fund_id: int
    recency_band: str | None
    value_band: str | None
    cadence_band: str | None
    hold_band: str | None
    message_angle: str | None
    priority_tier: str | None
    call_brief: str | None = None


class ClientIdentityOut(BaseModel):
    """Identity and product, straight off clients/client_features/funds.

    client_id and client_code are pseudonymous re-attachment keys and stay
    restricted from the model boundary; showing them here widens what this
    console displays, not what any prompt may cite.
    """

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
    """Boolean facts about this client's history.

    history_censored belongs next to any count in this profile: 83.7 percent
    of relationships have a capped purchase history, and a count shown
    without this flag reads as a complete one.
    """

    in_wave: bool | None
    newly_dormant: bool | None
    has_depth: bool | None
    staged_exit: bool | None
    stale_contact: bool | None
    history_censored: bool | None
    purchases_censored: bool | None


class ClientActivityOut(BaseModel):
    """What this client did, in KES -- see ClientFlagsOut.history_censored
    before reading n_purchases_returned or observed_volume as a complete count.
    """

    last_activity_date: date | None
    days_since_last_activity: int | None
    observed_volume: int | None
    n_purchases_returned: int | None
    total_purchase_amount: float | None
    computed_at: str | None


class ClientRoutingOut(BaseModel):
    """The angle and tier this client resolved to, and the rule that produced it."""

    message_angle: str | None
    priority_tier: str | None
    urgency: str | None
    prompt_variant: str | None
    rule_name: str | None
    rule_version: int | None


class ClientEnrollmentOut(BaseModel):
    """One campaign this client is or was enrolled in."""

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
    """One outreach_message's status history. Never the drafted or
    personalized content -- personalized_content carries the re-attached
    name, which this profile does not expose (see the router docstring).
    """

    message_id: str
    campaign_id: int
    template_id: str | None
    channel: str
    status: str
    created_at: datetime
    updated_at: datetime


class ClientContactEventOut(BaseModel):
    """One inbound signal: a reply, open, bounce, complaint, or opt-out."""

    id: int
    type: str
    occurred_at: datetime
    created_at: datetime


class ClientSuppressionOut(BaseModel):
    """Whether this client is suppressed, and why, if so."""

    is_suppressed: bool
    reason: str | None
    source: str | None
    created_at: datetime | None


class ClientProfileOut(BaseModel):
    """The fuller, non-PII client profile: identity, bands, flags, activity,
    routing, and every campaign/engagement record. No name -- that is
    ClientNameOut, a separate, gated endpoint (see the router docstring).
    """

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
    """The one PII field this console withholds everywhere else. Gated
    behind the reviewer key and audited on every read (see the router).
    """

    client_id: int
    client_name: str | None


class SegmentBucketOut(BaseModel):
    key: str | None
    count: int


class SegmentDistributionOut(BaseModel):
    by_purchase_depth: list[SegmentBucketOut]
    by_value_band: list[SegmentBucketOut]
    by_message_angle: list[SegmentBucketOut]
    # A contact over three years stale never blocks a send; this is visibility
    # into the ramp a batch should ease into, not a count of anything held.
    stale_contact_count: int
