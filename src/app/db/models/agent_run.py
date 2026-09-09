"""One pass of the proactive agent, and every tool it called along the way.

agent_run is the record of one loop from start to finish: when it started and
stopped, what state it ended in, what triggered it, the plan it wrote, the
summary it produced, and what it cost. agent_tool_call is the ordered list of
every tool call made during one run. It is kept apart from tool_calls, which
belongs to a drafting run, so each table stays about exactly one kind of run.
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AGENT_RUN_STATES = ("running", "completed", "failed")

AGENT_RUN_TRIGGERS = ("nightly", "manual", "chat")

NIGHTLY_AGENT = "nightly"
INTELLIGENCE_AGENT = "intelligence"
ACTION_AGENT = "action"

# Which agent the run belongs to. The two that read the whole book take a
# while and only one of them may go at a time; an action run answers one
# accepted finding, so it runs whenever a person asks for it.
AGENT_KINDS = (NIGHTLY_AGENT, INTELLIGENCE_AGENT, ACTION_AGENT)

BOOK_WIDE_AGENTS = (NIGHTLY_AGENT, INTELLIGENCE_AGENT)


class AgentRun(Base):
    """One run of the agent loop, from start to finish."""

    __tablename__ = "agent_run"
    __table_args__ = (
        CheckConstraint("state IN ('running', 'completed', 'failed')", name="ck_agent_run_state"),
        CheckConstraint("trigger IN ('nightly', 'manual', 'chat')", name="ck_agent_run_trigger"),
        CheckConstraint("cost_kes IS NULL OR cost_kes >= 0", name="ck_agent_run_cost_not_negative"),
        CheckConstraint(
            "agent_kind IN ('nightly', 'intelligence', 'action')",
            name="ck_agent_run_agent_kind",
        ),
    )

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    agent_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=NIGHTLY_AGENT)
    # The finding an action run was started for. Null for the other agents.
    insight_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_insight.insight_id"), nullable=True, index=True
    )
    risk_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("risk_run.run_id", ondelete="SET NULL"), nullable=True
    )
    plan_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentToolCall(Base):
    """One tool call made during one agent run, in the order it happened."""

    __tablename__ = "agent_tool_call"
    __table_args__ = (UniqueConstraint("run_id", "ordinal", name="uq_agent_tool_call_run_ordinal"),)

    tool_call_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_run.run_id"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tool_output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
