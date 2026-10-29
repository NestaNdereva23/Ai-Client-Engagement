from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CAMPAIGN_RUN_STATES = ("running", "completed", "failed")
PLAN_RUN = "plan"
SUGGEST_RUN = "suggest"
CAMPAIGN_RUN_KINDS = (PLAN_RUN, SUGGEST_RUN)


class CampaignAgentRun(Base):
    __tablename__ = "campaign_agent_run"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'completed', 'failed')", name="ck_campaign_agent_run_state"
        ),
        CheckConstraint("kind IN ('plan', 'suggest')", name="ck_campaign_agent_run_kind"),
        CheckConstraint(
            "cost_kes IS NULL OR cost_kes >= 0", name="ck_campaign_agent_run_cost_not_negative"
        ),
        # One running run per kind and campaign, whatever two requests do at once.
        Index(
            "uq_campaign_agent_run_one_running",
            "kind",
            text("COALESCE(campaign_id, 0)"),
            unique=True,
            postgresql_where=text("state = 'running'"),
        ),
    )

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id", ondelete="SET NULL"), index=True
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A plan run's counts and the checks that sent a plan back; null for a suggestion run.
    plan_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class CampaignAgentToolCall(Base):
    __tablename__ = "campaign_agent_tool_call"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_campaign_agent_tool_call_run_ordinal"),
    )

    tool_call_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("campaign_agent_run.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tool_output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
