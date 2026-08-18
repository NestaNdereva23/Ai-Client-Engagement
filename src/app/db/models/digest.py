"""The morning digest tables: digest_run, digest_line.

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
    """One client-fund's line within one FA's (or fund's) group, capped and
    ranked within that group by fund_at_risk.

    group_key is "fa:<fa_id>" or "fund:<unit_fund_id>", matching the
    FaAssignment fallback: grouped by fund whenever fa_id is null. Every line
    in a group repeats the same group_total, the count of eligible rows
    before the per-group cap was applied, so a caller can render "and N
    more" without a second query.
    """

    __tablename__ = "digest_line"
    __table_args__ = (Index("ix_digest_line_run_group_rank", "digest_run_id", "group_key", "rank"),)

    line_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("digest_run.digest_run_id"), nullable=False
    )
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    group_total: Mapped[int] = mapped_column(Integer, nullable=False)
    # The true fund_at_risk sum across every eligible row in this group, not
    # just the ones the per-group cap kept -- duplicated onto every line in
    # a group, the same way group_total already is.
    group_fund_value_total: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)

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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
