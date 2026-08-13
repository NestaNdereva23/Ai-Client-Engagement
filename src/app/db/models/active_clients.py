"""The active book: active_client_fund, the active-client counterpart of client_fund.

Kept separate from client_fund rather than merged into it, because the
dormant table's columns describe a relationship that has already ended; an
active relationship has no exit yet, so a shared schema would leave half its
columns meaningless on every row.

Ingestion fills the directly observed columns: balance, purchase/sale counts
and dates, censoring flags, computed_at. transform/active_features.py derives
the rest (rhythm_days, ticket_trend, and the others below) from the same
transactions on every transform run; a row stays null on those columns only
until the first successful transform.
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

INTERACTION_TYPES = ("call_logged", "snoozed", "dismissed")


class ActiveClientFund(Base):
    """One row per client-fund relationship in the active book."""

    __tablename__ = "active_client_fund"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_purchases: Mapped[int] = mapped_column(Integer, nullable=False)
    n_sales: Mapped[int] = mapped_column(Integer, nullable=False)
    last_purchase: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_sale: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchases_censored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # True when the sale window is full, so older redemptions are hidden --
    # the active-book counterpart of client_fund.history_censored.
    redemption_history_blind: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Derived measures, computed by transform/active_features.py from the
    # client's own transactions. Null here means "not yet computed", not
    # "zero".
    rhythm_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_ticket: Mapped[float | None] = mapped_column(Float, nullable=True)
    ticket_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A genuine client-initiated redemption, filtered to exclude system-driven
    # sales (the SYSTEM_SALE_MAX heuristic).
    largest_real_sale: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The most recent date among real sales, not among every sale slot --
    # see transform/active_features.py::_last_real_sale_date.
    last_real_sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fee_runway_months: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ActiveClientInteraction(Base):
    """One FA action logged against an active-client digest line: a call,
    a snooze, or a dismiss (see the reference reviewer console). Manual
    bookkeeping only -- the active-client population has no campaign or
    enrollment path in this codebase yet, so nothing here triggers a send
    or a routing change; it only records that a human already acted.
    """

    __tablename__ = "active_client_interaction"
    __table_args__ = (
        CheckConstraint(
            "type IN ('call_logged', 'snoozed', 'dismissed')",
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
