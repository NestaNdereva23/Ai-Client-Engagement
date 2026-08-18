"""The active book: active_client_fund, the active-client counterpart of client_fund.

Kept separate from client_fund rather than merged into it, because the
dormant table's columns describe a relationship that has already ended; an
active relationship has no exit yet, so a shared schema would leave half its
columns meaningless on every row.

Ingestion fills the directly observed columns: balance, deposit/withdrawal
counts and dates, capping flags, computed_at. transform/active_features.py
derives the rest (typical_gap_days, deposit_trend, and the others below)
from the same transactions on every transform run; a row stays null on
those columns only until the first successful transform.
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
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INTERACTION_TYPES = ("call_logged", "snoozed", "dismissed", "email_sent")


class ActiveClientFund(Base):
    """One row per client-fund relationship in the active book."""

    __tablename__ = "active_client_fund"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_deposits: Mapped[int] = mapped_column(Integer, nullable=False)
    n_withdrawals: Mapped[int] = mapped_column(Integer, nullable=False)
    last_deposit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_withdrawal_slot_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deposit_count_capped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # True when the withdrawal window is full, so older withdrawals are
    # hidden -- the active-book counterpart of client_fund.history_censored.
    withdrawal_history_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Derived measures, computed by transform/active_features.py from the
    # client's own transactions. Null here means "not yet computed", not
    # "zero".
    typical_gap_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_deposit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_deposit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_deposit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    deposit_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A genuine client-initiated withdrawal, filtered to exclude
    # system-driven withdrawals (the SYSTEM_FEE_MAX rule of thumb).
    largest_withdrawal: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The most recent date among real withdrawals, not among every
    # withdrawal slot -- see transform/active_features.py::_last_withdrawal_date.
    last_withdrawal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    months_until_empty: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ActiveClientInteraction(Base):
    """One FA action logged against an active-client digest line: a call,
    a snooze, a dismiss, or an email sent (see the reference reviewer
    console). Manual bookkeeping only -- the active-client population has
    no campaign or enrollment path in this codebase yet, so nothing here
    triggers a send or a routing change; it only records that a human
    already acted.
    """

    __tablename__ = "active_client_interaction"
    __table_args__ = (
        CheckConstraint(
            "type IN ('call_logged', 'snoozed', 'dismissed', 'email_sent')",
            name="ck_active_client_interaction_type",
        ),
        Index(
            "ix_active_client_interaction_client_fund", "client_id", "unit_fund_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The reviewer X-Reviewer-Key resolved to (app.api.reviewer_auth), never
    # a self-reported field on the request body.
    reviewer_id: Mapped[str] = mapped_column(Text, nullable=False)
    # client_risk_features.risk_band for this client-fund at the moment this
    # row was written, or null if it had never been scored yet. Lets the
    # digest build tell whether a client got worse after an FA manager acted
    # on them, without a second nearest-snapshot lookup -- see
    # digest/build.py::_is_deprioritized.
    risk_band_at_interaction: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActiveTransaction(Base):
    """One purchase or sale observed for an active-book client-fund.

    Upserted on txn_id (the source's own id, treated as globally unique the
    same way the dormant Transactions table treats it) on every transform
    run, so a transaction that ages out of the feed's own "last 5
    purchases" / "last 2 sales" window on a later pull stays visible here
    rather than disappearing -- this table accumulates across nightly runs,
    the feed itself never does. Still not a claim of full lifetime history:
    active_client_fund.deposit_count_capped / withdrawal_history_hidden says
    whether even the most recent pull was itself capped, and a transaction
    that aged out before this table started accumulating is gone for good.

    No foreign key to active_client_fund: ingestion and transform can see a
    transaction for a client-fund before or without a settled
    active_client_fund row for it (the same reasoning client_fund's sibling
    Transactions table follows for `clients`).
    """

    __tablename__ = "active_transaction"
    __table_args__ = (
        Index("ix_active_transaction_client_fund", "client_id", "unit_fund_id", "txn_date"),
    )

    txn_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    txn_type: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fund_short_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_incurred: Mapped[float | None] = mapped_column(Float, nullable=True)
    sale_type: Mapped[str | None] = mapped_column(Text, nullable=True)
