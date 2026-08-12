"""Tables for the ingestion stage.

raw_staging keeps the exact response before any parsing, so we can re-process
without calling the source again. ingestion_status tracks each run so it can
resume where it stopped. ingestion_rejects stores records that failed validation
along with the reason, so one bad record does not stop the run.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Allowed values for the run state.
INGESTION_STATES = ("running", "completed", "failed")


class RawStaging(Base):
    """One captured response for a run, stored exactly as received.

    natural_key names the piece within a run (the page cursor). Making
    run_id plus natural_key unique lets a repeated page overwrite its row
    instead of adding a duplicate.
    """

    __tablename__ = "raw_staging"
    __table_args__ = (UniqueConstraint("run_id", "natural_key", name="uq_raw_staging_run_natural"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    natural_key: Mapped[str] = mapped_column(Text, nullable=False)
    pulled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class IngestionStatus(Base):
    """Progress and counters for one run, so it can resume after a failure.

    fund_cursor and page_cursor hold the last fund and page that finished, so a
    resumed run carries on after the last saved page instead of starting over.
    The counters summarise what the run has seen and written.
    """

    __tablename__ = "ingestion_status"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'completed', 'failed')",
            name="ck_ingestion_status_state",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    fund_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Anchor for recency math. Set once when the run starts and never on resume,
    # so transform derives the same days_since_* however often it re-runs.
    reference_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    records_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Extra counters so data quality is visible without another query.
    records_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    shortfall: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class IngestionReject(Base):
    """A record that failed validation, kept together with why it failed."""

    __tablename__ = "ingestion_rejects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    raw_fragment: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Funds(Base):
    """A unit fund and how many dormant clients it holds.

    Keyed on the source unit_fund_id so re-running the transform upserts in place
    instead of duplicating.
    """

    __tablename__ = "funds"

    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_name: Mapped[str] = mapped_column(Text, nullable=False)
    inactive_client_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Clients(Base):
    """One row per client, holding the figures of their largest relationship.

    A client who holds several funds still gets a single row here, because a
    person receives one message however many funds they held. n_funds says how
    many they have; the per-fund detail is in client_fund.
    """

    __tablename__ = "clients"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_fund_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("funds.unit_fund_id"), nullable=False, index=True
    )
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_purchases_returned: Mapped[int] = mapped_column(Integer, nullable=False)
    n_sales_returned: Mapped[int] = mapped_column(Integer, nullable=False)
    last_purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_purchase_amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    total_sale_amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    last_activity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_since_last_activity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many funds this client held. Their figures above come from the largest.
    n_funds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # True when the purchase window is full, so older purchases are hidden.
    purchases_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class ClientFund(Base):
    """One row per client-fund relationship, holding the figures of that fund."""

    __tablename__ = "client_fund"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), primary_key=True, autoincrement=False
    )
    unit_fund_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("funds.unit_fund_id"), primary_key=True, autoincrement=False
    )
    client_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_purchases: Mapped[int] = mapped_column(Integer, nullable=False)
    n_sales: Mapped[int] = mapped_column(Integer, nullable=False)
    last_purchase: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_sale: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The later of the two dates above: when this relationship went quiet.
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_cold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Purchase value we can see. A floor, not a total, since older purchases
    # fall outside the window the source returns.
    observed_volume: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    purchases_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    history_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Set on the largest of a client's relationships, so one person is
    # approached once rather than once per fund they happen to hold.
    is_primary_contact_row: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Measures of how this relationship was used. Null where the transactions
    # cannot support them, which the bands read as unknown rather than as zero.
    avg_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Median gap between purchases. Under a day means same-day top-ups.
    rhythm_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_purchase: Mapped[date | None] = mapped_column(Date, nullable=True)
    # First to last visible purchase.
    active_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Slope of log10 contribution size: positive means contributions were rising.
    ticket_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_sale: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Gap between the two visible sales; a wide one is a staged wind-down.
    drawdown_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How long the money stayed after the final top-up.
    hold_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How the final sale was recorded, which says whether leaving was a choice.
    exit_type: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Transactions(Base):
    __tablename__ = "transactions"

    txn_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    txn_type: Mapped[str] = mapped_column(String(16), nullable=False)
    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), nullable=False, index=True
    )
    unit_fund_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    fund_short_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_incurred: Mapped[float | None] = mapped_column(Float, nullable=True)
    sale_type: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClientFeatures(Base):
    """Bucketed, model-safe features for one client, keyed by client_id.

    Every column is a label, bucket, count, or interval, never an exact amount or
    date, so the model-facing projection can allow-list straight from here. Rows
    are recomputed by the pipeline, not edited by hand.
    """

    __tablename__ = "client_features"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), primary_key=True, autoincrement=False
    )
    own_rhythm_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_volume: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    purchases_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    history_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # Behavioural bands describing the relationship this client is contacted on.
    # Each carries an explicit unknown member rather than a null, so a rule that
    # names a band never has to reason about a missing value.
    n_funds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    recency_band: Mapped[str] = mapped_column(Text, nullable=False, server_default="Unknown")
    value_band: Mapped[str] = mapped_column(Text, nullable=False, server_default="Low")
    cadence_band: Mapped[str] = mapped_column(Text, nullable=False, server_default="None")
    hold_band: Mapped[str] = mapped_column(Text, nullable=False, server_default="Unknown")
    purchase_depth: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    trend_band: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    exit_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    fund_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="other")
    in_wave: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    has_depth: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    staged_exit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    stale_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    newly_dormant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    holds_other_funds: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Derived from value_band and recency_band, not set by a rule.
    priority_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default="T4")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PiiVault(Base):
    """The only table holding real PII, keyed by client_id for re-attachment."""

    __tablename__ = "pii_vault"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_whatsapp: Mapped[str | None] = mapped_column(Text, nullable=True)
    opt_out_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
