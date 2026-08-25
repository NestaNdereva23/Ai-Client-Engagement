"""The morning digest tables: digest_run, digest_line, digest_email_send.

A digest is generated once at the end of a successful nightly risk run
(app/workers/digest.py) and persisted, not only computed on request, so
there is a durable record of what an FA or team lead actually saw on a given
morning. GET /digest/{fa_or_fund_key} serves the persisted lines for the
current day's most recent digest_run.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class DigestRun(Base):
    """One digest generation, tied to the risk_run it was built from."""

    __tablename__ = "digest_run"

    digest_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    risk_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("risk_run.run_id"), nullable=False, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DigestLine(Base):
    __tablename__ = "digest_line"
    __table_args__ = (Index("ix_digest_line_run_group_rank", "digest_run_id", "group_key", "rank"),)

    line_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("digest_run.digest_run_id"), nullable=False
    )
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    group_total: Mapped[int] = mapped_column(Integer, nullable=False)
    group_fund_value_total: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # rank floor-divided by that run's cap_per_group. Which batch a row
    # belongs to; see the class docstring.
    batch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_band: Mapped[str] = mapped_column(Text, nullable=False)
    risk_reasons: Mapped[str] = mapped_column(Text, nullable=False)
    # risk_reasons split into short codes (fired signal names, "sig_"
    # stripped), so a console can render chips without parsing prose.
    risk_reason_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    fund_at_risk: Mapped[float] = mapped_column(Float, nullable=False)
    # None when there is no prior run to compare against, never a fabricated
    # zero -- the same rule risk/history.py::delta_for already follows.
    score_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)

    route: Mapped[str] = mapped_column(Text, nullable=False)
    in_call_queue: Mapped[bool] = mapped_column(Boolean, nullable=False)
    complaint_caveat: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # True when an FA manager already acted on this client-fund (call,
    # snooze, dismiss, or email) and their risk band hasn't risen since
    deprioritized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    covering_for_fa_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DigestEmailSend(Base):
    __tablename__ = "digest_email_send"
    __table_args__ = (
        UniqueConstraint("digest_run_id", "fa_id", name="uq_digest_email_send_run_fa"),
    )

    send_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("digest_run.digest_run_id"), nullable=False, index=True
    )
    fa_id: Mapped[str] = mapped_column(Text, nullable=False)
    # "sent" when it went out over SMTP, "recorded" when no mail server was
    # configured and the null mailer took it instead.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    client_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fund_value_total: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
