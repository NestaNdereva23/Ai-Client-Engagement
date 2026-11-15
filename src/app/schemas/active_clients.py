"""Request and response shapes for the active-client endpoints."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.db.models.active_clients import (
    CHANNEL_BY_INTERACTION_TYPE,
    CONTACT_INTERACTION_TYPES,
)
from app.schemas.client_tiers import ClientTierDetailOut, ClientTierTagOut

InteractionType = Literal[
    "call_logged",
    "email_sent",
    "meeting_logged",
    "whatsapp_logged",
    "snoozed",
    "dismissed",
]
InteractionOutcome = Literal["connected", "follow_up", "no_answer"]
TouchpointKind = Literal["call", "meeting", "email", "reminder"]


class InteractionCreate(BaseModel):
    type: InteractionType
    outcome: InteractionOutcome | None = None
    note: str | None = None
    follow_up_on: date | None = None
    touchpoint_id: int | None = None

    @model_validator(mode="after")
    def _contact_fields_go_with_a_contact(self) -> InteractionCreate:
        is_contact = self.type in CONTACT_INTERACTION_TYPES
        if not is_contact and (self.outcome is not None or self.touchpoint_id is not None):
            raise ValueError("outcome and touchpoint_id are only for a logged contact")
        return self

    @model_validator(mode="after")
    def _follow_up_day_goes_with_a_snooze_or_follow_up(self) -> InteractionCreate:
        needs_day = self.type == "snoozed" or self.outcome == "follow_up"
        if self.follow_up_on is not None and not needs_day:
            raise ValueError("follow_up_on is only for a snooze or a follow-up outcome")
        return self


class InteractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    unit_fund_id: int
    type: str
    outcome: str | None
    note: str | None
    reviewer_id: str
    risk_band_at_interaction: str | None
    created_at: datetime

    @computed_field
    @property
    def channel(self) -> str | None:
        return CHANNEL_BY_INTERACTION_TYPE.get(self.type)


class TouchpointCreate(BaseModel):
    kind: TouchpointKind
    title: str = Field(min_length=1, max_length=200)
    due_date: date
    due_time: time | None = None
    minutes: int | None = Field(default=None, ge=5, le=480)

    @model_validator(mode="after")
    def _minutes_only_for_a_call_or_meeting(self) -> TouchpointCreate:
        if self.minutes is not None and self.kind not in ("call", "meeting"):
            raise ValueError("minutes is only for a call or a meeting")
        return self


class TouchpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    unit_fund_id: int
    kind: str
    title: str
    due_date: date
    due_time: time | None
    minutes: int | None
    status: str
    created_by: str
    created_at: datetime


class NextActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str
    kind: str
    why: str


class ActiveClientIdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_code: str | None
    fund_name: str
    fund_short_name: str
    balance: float | None
    # Whether even the most recent pull was itself capped at "last 5
    # purchases" / "last 2 sales" -- the caveat a ledger/money view needs
    # before treating `transactions` as a complete history.
    deposit_count_capped: bool
    withdrawal_history_hidden: bool


class ActiveClientBandsOut(BaseModel):
    """The current-state bands and score client_risk_features holds for
    this client-fund. Every field is null when no nightly run has scored
    it yet, not a 404 -- see ActiveClientProfileOut.
    """

    model_config = ConfigDict(from_attributes=True)

    recency_band: str | None
    balance_tier: str | None
    value_tier: str | None
    pattern_is_reliable: bool
    risk_score: int | None
    risk_band: str | None
    risk_reasons: str | None
    # risk_reasons split into short codes (fired signal names, "sig_"
    # stripped) -- computed the same way DigestLineOut.risk_reason_tags is,
    # via app.risk.signals.fired_signal_tags, so a client-fund reads the
    # same tags whether it showed up in today's digest or was opened here
    # directly. Empty when there is no risk row yet, not a 404.
    risk_reason_tags: list[str]
    route: str | None
    fund_at_risk: float | None
    # The label and magnitude of whichever fired signal weighs heaviest in
    # this client's own risk_config_version, e.g. "Heavy withdrawal: 63% of
    # balance withdrawn at once". None when there is no risk row yet or
    # nothing fired -- see app.risk.magnitude.primary_signal_magnitude.
    primary_signal_magnitude: str | None
    deposit_trend_band: str | None = None


class ActiveClientRiskHistoryEntryOut(BaseModel):
    """One risk_snapshot row: this client-fund's numbers as of one nightly run."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    risk_score: int
    risk_band: str
    risk_reasons: str
    risk_reason_tags: list[str]
    route: str | None
    created_at: datetime


class ActiveClientComplaintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opened_at: date
    closed_at: date | None
    status: str
    category: str
    channel: str


class ActiveTransactionOut(BaseModel):
    """One deposit or withdrawal observed for this client-fund, accumulated
    across nightly pulls -- see app.db.models.active_clients.ActiveTransaction.
    Not a claim of full lifetime history: check
    ActiveClientIdentityOut.deposit_count_capped / withdrawal_history_hidden
    first.
    """

    model_config = ConfigDict(from_attributes=True)

    txn_id: int
    txn_type: str
    client_id: int
    unit_fund_id: int
    fund_short_name: str | None
    txn_date: date | None
    amount: float
    unit_price: float | None
    fees_incurred: float | None
    sale_type: str | None


class DepositPercentileOut(BaseModel):
    """Where this client-fund's observed lifetime deposit total ranks
    against the whole active book. Not a claim of full lifetime history --
    see deposit_count_capped before treating total_deposits as complete.
    """

    total_deposits: float
    book_size: int
    rank: int
    percentile: float | None
    deposit_count_capped: bool


class ActiveClientRosterLineOut(ClientTierTagOut):
    """One row of the paginated active-book roster. Null risk fields mean
    no nightly run has scored this client-fund yet, not a gap.
    """

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_code: str | None
    fund_name: str
    fund_short_name: str
    balance: float | None
    risk_band: str | None
    risk_score: int | None
    fund_at_risk: float | None
    route: str | None
    # Fired signal short codes, computed the same way
    # ActiveClientBandsOut.risk_reason_tags is -- empty when there is no
    # risk row yet, not a gap.
    risk_reason_tags: list[str]
    # Same computation as ActiveClientBandsOut.primary_signal_magnitude --
    # None when there is no risk row yet or nothing fired.
    primary_signal_magnitude: str | None
    # Whether this client has an open complaint -- the same flag
    # DigestLineOut.complaint_caveat carries, so a reviewer reads the same
    # caution mark whether the line came from today's digest or the roster.
    complaint_caveat: bool
    # When an FA last logged a call/snooze/dismiss against this client-fund,
    # or None if nothing has been logged yet.
    last_interaction_at: datetime | None
    # Whether AM11's on-demand briefing has enough data to render for this
    # client-fund right now -- the same cheap existence check
    # DigestLineOut.briefing_available uses.
    briefing_available: bool
    # The agent's open suggestion for this client-fund, or None.
    next_action: NextActionOut | None = None
    next_action_note: str | None = None
    brief_group: str | None = None


class RouteChangeRunOut(BaseModel):
    """One completed nightly risk run's route-churn summary, read from its
    audit_log entry: how many client-funds got a different route than the
    run before it, alongside the run's own coverage and route mix.

    more_urgent_count + less_urgent_count does not always equal
    routes_changed -- a client's first-ever scored run has no from_route to
    compare against (app.risk.routing.route_direction returns None), so
    it's counted in routes_changed but neither bucket here.
    """

    run_id: str | None
    as_of: datetime
    clients_seen: int
    routes_changed: int
    route_distribution: dict[str, int]
    more_urgent_count: int
    less_urgent_count: int


class ReviewPeriodActivityOut(BaseModel):
    review_period_days: int
    deposits: int
    withdrawals: int


class ActiveClientProfileOut(BaseModel):
    """Everything this codebase holds about one active-client-fund
    relationship, short of a name -- the active-book counterpart of
    ClientProfileOut (app.schemas.clients).
    """

    identity: ActiveClientIdentityOut
    bands: ActiveClientBandsOut
    risk_history: list[ActiveClientRiskHistoryEntryOut]
    complaints: list[ActiveClientComplaintOut]
    interactions: list[InteractionOut]
    transactions: list[ActiveTransactionOut]
    segment_and_tier: ClientTierDetailOut
    next_action: NextActionOut | None = None
    next_action_note: str | None = None
    review_activity: ReviewPeriodActivityOut | None = None


class TransactionMonthOut(BaseModel):
    """One month's deposit or withdrawal volume, book-wide."""

    month: date
    txn_type: str
    count: int
    total_amount: float
    avg_fees: float | None


class SaleTypeBucketOut(BaseModel):
    """One sale_type's share of every observed withdrawal, book-wide."""

    sale_type: str | None
    count: int
    total_amount: float


class TransactionAnalyticsOut(BaseModel):
    """Book-wide transaction patterns: deposit/withdrawal volume by month
    over the trailing window, and a sale_type breakdown among withdrawals.
    Read from active_transaction, the accumulated observed history -- see
    that table's own docstring for what "observed" does and doesn't cover.
    """

    by_month: list[TransactionMonthOut]
    by_sale_type: list[SaleTypeBucketOut]
