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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SIGNAL_RUN_STATES = ("running", "completed", "failed")


class SignalRun(Base):
    __tablename__ = "signal_run"
    __table_args__ = (
        CheckConstraint("state IN ('running', 'completed', 'failed')", name="ck_signal_run_state"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientSignalSnapshot(Base):
    __tablename__ = "client_signal_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "client_id",
            "unit_fund_id",
            "signal_code",
            name="uq_client_signal_snapshot_run_client_fund_signal",
        ),
        Index(
            "ix_client_signal_snapshot_client_fund_signal_run",
            "client_id",
            "unit_fund_id",
            "signal_code",
            "run_id",
        ),
    )

    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("signal_run.run_id"), nullable=False)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signal_code: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClientSignalState(Base):
    __tablename__ = "client_signal_state"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    signal_code: Mapped[str] = mapped_column(Text, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    since: Mapped[date] = mapped_column(Date, nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("signal_run.run_id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ClientSituationSnapshot(Base):
    __tablename__ = "client_situation_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "client_id",
            "unit_fund_id",
            "situation_code",
            name="uq_client_situation_snapshot_run_client_fund_situation",
        ),
        Index(
            "ix_client_situation_snapshot_client_fund_situation_run",
            "client_id",
            "unit_fund_id",
            "situation_code",
            "run_id",
        ),
    )

    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("signal_run.run_id"), nullable=False)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    situation_code: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    signal_codes: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClientSituationState(Base):
    __tablename__ = "client_situation_state"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    situation_code: Mapped[str] = mapped_column(Text, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    signal_codes: Mapped[list] = mapped_column(JSONB, nullable=False)
    since: Mapped[date] = mapped_column(Date, nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("signal_run.run_id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SignalThreshold(Base):
    __tablename__ = "signal_threshold"
    __table_args__ = (
        UniqueConstraint(
            "version",
            "signal_code",
            "threshold_name",
            name="uq_signal_threshold_version_signal_code_threshold_name",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_signal_threshold_status",
        ),
    )

    threshold_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    signal_code: Mapped[str] = mapped_column(Text, nullable=False)
    threshold_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
