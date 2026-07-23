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
    net_flow: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    computed_at: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class PiiVault(Base):
    """The only table holding real PII, keyed by client_id for re-attachment.

    client_name is the sole real personal data in the source. Contact channels
    are not in the payload yet; the columns exist and are nullable so a contact
    source slots in later with no schema change. Kept standalone with no foreign
    key so it can move to a restricted DB role that the generation service
    cannot read.
    """

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
