"""What the agent found, the facts behind it, and who it is about.

agent_insight is one finding: what kind it is, the group it is about, how
big that group is, and what the model reads into it. The numbers it rests
on are kept apart in agent_insight_fact, one row per fact, each carrying
the filter and the table it was counted from, so any figure on the screen
can be run again and checked. agent_insight_client is the exact list of
client funds the finding covers, so a later outcome can be matched back to
the finding that caused it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INSIGHT_KINDS = (
    "risk",
    "opportunity",
    "lifecycle_change",
    "anomaly",
    "pattern",
    "campaign",
)

# One finding's life, in the order it is normally reached. See
# app.agents.insight_state for which moves between these are allowed.
INSIGHT_STATES = (
    "new",
    "accepted",
    "acted_on",
    "dismissed",
    "expired",
)

INSIGHT_CONFIDENCE_LEVELS = ("high", "medium", "low")


class AgentInsight(Base):
    """One thing the agent noticed, and what it suggests doing about it."""

    __tablename__ = "agent_insight"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('risk', 'opportunity', 'lifecycle_change', 'anomaly', 'pattern', 'campaign')",
            name="ck_agent_insight_kind",
        ),
        CheckConstraint(
            "state IN ('new', 'accepted', 'acted_on', 'dismissed', 'expired')",
            name="ck_agent_insight_state",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_agent_insight_confidence",
        ),
        CheckConstraint(
            "state <> 'dismissed' OR dismissed_reason IS NOT NULL",
            name="ck_agent_insight_dismissed_reason_required",
        ),
        CheckConstraint("client_count >= 0", name="ck_agent_insight_client_count_not_negative"),
        CheckConstraint(
            "money_total_kes IS NULL OR money_total_kes >= 0",
            name="ck_agent_insight_money_total_not_negative",
        ),
    )

    insight_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_run.run_id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Allow listed filters, the same shape as campaign.cohort_definition.
    group_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    client_count: Mapped[int] = mapped_column(Integer, nullable=False)
    money_total_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_reason: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    # What a message about this finding must not claim.
    avoid_saying: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_now: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    # Required whenever the state is dismissed. Null otherwise.
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentInsightFact(Base):
    """One number the finding rests on, with the filter it was counted from."""

    __tablename__ = "agent_insight_fact"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(source_filter) = 'object' AND source_filter <> '{}'::jsonb",
            name="ck_agent_insight_fact_source_filter_required",
        ),
        CheckConstraint(
            "length(btrim(source_table)) > 0",
            name="ck_agent_insight_fact_source_table_required",
        ),
    )

    fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    insight_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("agent_insight.insight_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_text: Mapped[str] = mapped_column(Text, nullable=False)
    fact_value: Mapped[str] = mapped_column(Text, nullable=False)
    source_filter: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)


class AgentInsightClient(Base):
    """One client fund the finding covers."""

    __tablename__ = "agent_insight_client"
    __table_args__ = (
        UniqueConstraint(
            "insight_id",
            "client_id",
            "unit_fund_id",
            name="uq_agent_insight_client_insight_client_fund",
        ),
    )

    insight_client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    insight_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("agent_insight.insight_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
