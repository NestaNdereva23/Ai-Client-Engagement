"""Request and response shapes for the active-client endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

InteractionType = Literal["call_logged", "snoozed", "dismissed"]


class InteractionCreate(BaseModel):
    """One FA action logged against an active-client digest line."""

    type: InteractionType
    note: str | None = None


class InteractionOut(BaseModel):
    """One logged interaction, as stored."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    unit_fund_id: int
    type: str
    note: str | None
    reviewer_id: str
    created_at: datetime


class ActiveClientIdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_code: str | None
    fund_name: str
    balance: float | None
    # Whether even the most recent pull was itself capped at "last 5
    # purchases" / "last 2 sales" -- the caveat a ledger/money view needs
    # before treating `transactions` as a complete history.
    purchases_censored: bool
    redemption_history_blind: bool


class ActiveClientBandsOut(BaseModel):
    """The current-state bands and score client_risk_features holds for
    this client-fund. Every field is null when no nightly run has scored
    it yet, not a 404 -- see ActiveClientProfileOut.
    """

    model_config = ConfigDict(from_attributes=True)

    recency_band: str | None
    balance_tier: str | None
    value_tier: str | None
    credible_rhythm: bool
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
    aum_at_risk: float | None
    # The label and magnitude of whichever fired signal weighs heaviest in
    # this client's own risk_config_version, e.g. "Heavy redemption: 63% of
    # balance withdrawn in one sale". None when there is no risk row yet or
    # nothing fired -- see app.risk.magnitude.primary_signal_magnitude.
    primary_signal_magnitude: str | None


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
    """One purchase or sale observed for this client-fund, accumulated
    across nightly pulls -- see app.db.models.active_clients.ActiveTransaction.
    Not a claim of full lifetime history: check
    ActiveClientIdentityOut.purchases_censored / redemption_history_blind
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


class ContributionPercentileOut(BaseModel):
    """Where this client-fund's observed lifetime purchase total ranks
    against the whole active book. Not a claim of full lifetime history --
    see purchases_censored before treating total_contribution as complete.
    """

    total_contribution: float
    book_size: int
    rank: int
    percentile: float | None
    purchases_censored: bool


class ActiveClientRosterLineOut(BaseModel):
    """One row of the paginated active-book roster. Null risk fields mean
    no nightly run has scored this client-fund yet, not a gap.
    """

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    client_code: str | None
    fund_name: str
    balance: float | None
    risk_band: str | None
    risk_score: int | None
    aum_at_risk: float | None
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


class RouteChangeDetailOut(BaseModel):
    """One client-fund's route move within a single nightly run.

    direction is "more_urgent" (moved to a route that needs a human sooner)
    or "less_urgent" (moved to a route that needs a human less urgently),
    or None for a client's first-ever scored run, when there's no
    from_route to compare against. This is about queue priority only, not
    the client's own situation -- a move to dust_cleanup is "less_urgent"
    even when it means the client already withdrew almost everything.
    from_risk_band is the risk band this client-fund carried before this
    run; None on a first-ever scored run, same as from_route.
    """

    client_id: int
    unit_fund_id: int
    client_code: str | None
    fund_name: str
    from_route: str | None
    to_route: str
    direction: str | None
    from_risk_band: str | None
    risk_band: str
    reasons: str


class RouteChangeDetailsOut(BaseModel):
    """The client-level route moves behind one nightly run's routes_changed
    count, capped at `limit` per page (10 by default) with next_cursor set
    when there are more.
    """

    run_id: str | None
    as_of: datetime | None
    items: list[RouteChangeDetailOut]
    next_cursor: str | None = None


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


class TransactionMonthOut(BaseModel):
    """One month's purchase or sale volume, book-wide."""

    month: date
    txn_type: str
    count: int
    total_amount: float
    avg_fees: float | None


class SaleTypeBucketOut(BaseModel):
    """One sale_type's share of every observed sale, book-wide."""

    sale_type: str | None
    count: int
    total_amount: float


class TransactionAnalyticsOut(BaseModel):
    """Book-wide transaction patterns: purchase/sale volume by month over
    the trailing window, and a sale_type breakdown among sales. Read from
    active_transaction, the accumulated observed history -- see that
    table's own docstring for what "observed" does and doesn't cover.
    """

    by_month: list[TransactionMonthOut]
    by_sale_type: list[SaleTypeBucketOut]
