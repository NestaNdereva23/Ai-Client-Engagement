"""What the agent wants to do tonight, and to which clients.

A proposal is the record of one action for one group: the group it is for,
the evidence behind it, and what the permission setting or a person decided.
agent_proposal_client is the exact list the proposal was built from, so a
client who was left out always carries a reason on the record, not just in a
log line.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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

# One proposal's life, in the order it is normally reached. See
# app.agents.proposal_state for which moves between these are allowed.
PROPOSAL_STATUSES = (
    "proposed",
    "rejected",
    "expired",
    "approved",
    "running",
    "blocked",
    "sent",
    "stopped",
    "measured",
)


class AgentProposal(Base):
    """One action the agent wants to take, for one group of clients."""

    __tablename__ = "agent_proposal"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'rejected', 'expired', 'approved', 'running', "
            "'blocked', 'sent', 'stopped', 'measured')",
            name="ck_agent_proposal_status",
        ),
        CheckConstraint(
            "permission_applied IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_proposal_permission_applied",
        ),
        CheckConstraint(
            "content_mix IS NULL OR content_mix IN "
            "('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
            name="ck_agent_proposal_content_mix",
        ),
        CheckConstraint("client_count >= 0", name="ck_agent_proposal_client_count_not_negative"),
        CheckConstraint(
            "money_total_kes IS NULL OR money_total_kes >= 0",
            name="ck_agent_proposal_money_total_not_negative",
        ),
    )

    proposal_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_run.run_id"), nullable=True
    )
    action_code: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Allow listed filters, the same shape as campaign.cohort_definition.
    group_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    client_count: Mapped[int] = mapped_column(Integer, nullable=False)
    money_total_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Null for an action that sends no message.
    angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_mix: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which version of the message this group got, once a two version test is running.
    variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission_applied: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentProposalClient(Base):
    """One client the proposal considered, in or out, and the fund it is for."""

    __tablename__ = "agent_proposal_client"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "client_id",
            "unit_fund_id",
            name="uq_agent_proposal_client_proposal_client_fund",
        ),
        CheckConstraint(
            "included OR skip_reason IS NOT NULL",
            name="ck_agent_proposal_client_skip_reason_required",
        ),
    )

    proposal_client_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    proposal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_proposal.proposal_id"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Required whenever included is false. Null when the client was included.
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant: Mapped[str | None] = mapped_column(Text, nullable=True)
