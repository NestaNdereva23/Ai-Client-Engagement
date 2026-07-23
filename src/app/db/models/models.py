"""Tables for the ingestion stage.

raw_staging keeps the exact response before any parsing, so we can re-process
without calling the source again. ingestion_status tracks each run so it can
resume where it stopped. ingestion_rejects stores records that failed validation
along with the reason, so one bad record does not stop the run.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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
