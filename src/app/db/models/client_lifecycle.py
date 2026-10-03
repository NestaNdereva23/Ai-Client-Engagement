from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ACTIVE_STATE = "active"
DORMANT_STATE = "dormant"
LIFECYCLE_STATES = (ACTIVE_STATE, DORMANT_STATE)

AUTOMATIC = "automatic"
NEEDS_A_PERSON = "needs_a_person"
POLICY_MODES = (AUTOMATIC, NEEDS_A_PERSON)

PENDING = "pending"
NO_POLICY = "no_policy"
WAITING_FOR_A_PERSON = "waiting_for_a_person"
EVIDENCE_NOT_MET = "evidence_not_met"
APPLIED = "applied"
LIFECYCLE_OUTCOMES = (PENDING, NO_POLICY, WAITING_FOR_A_PERSON, EVIDENCE_NOT_MET, APPLIED)

_STATE_LIST = "('active', 'dormant')"


class LifecyclePolicy(Base):
    __tablename__ = "lifecycle_policy"
    __table_args__ = (
        UniqueConstraint("policy_code", name="uq_lifecycle_policy_code"),
        UniqueConstraint("from_state", "to_state", name="uq_lifecycle_policy_change"),
        CheckConstraint(f"from_state IN {_STATE_LIST}", name="ck_lifecycle_policy_from_state"),
        CheckConstraint(f"to_state IN {_STATE_LIST}", name="ck_lifecycle_policy_to_state"),
        CheckConstraint("from_state <> to_state", name="ck_lifecycle_policy_a_real_change"),
        CheckConstraint("mode IN ('automatic', 'needs_a_person')", name="ck_lifecycle_policy_mode"),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'object' AND evidence <> '{}'::jsonb",
            name="ck_lifecycle_policy_evidence_required",
        ),
    )

    policy_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_code: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    changed_reason: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ClientLifecycle(Base):
    __tablename__ = "client_lifecycle"
    __table_args__ = (CheckConstraint(f"state IN {_STATE_LIST}", name="ck_client_lifecycle_state"),)

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    workflow: Mapped[str] = mapped_column(Text, nullable=False)
    policy_code: Mapped[str] = mapped_column(Text, nullable=False)
    insight_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("agent_insight.insight_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentInsightLifecycle(Base):
    __tablename__ = "agent_insight_lifecycle"
    __table_args__ = (
        CheckConstraint(f"from_state IN {_STATE_LIST}", name="ck_insight_lifecycle_from_state"),
        CheckConstraint(f"to_state IN {_STATE_LIST}", name="ck_insight_lifecycle_to_state"),
        CheckConstraint("from_state <> to_state", name="ck_insight_lifecycle_a_real_change"),
        CheckConstraint(
            "outcome IN ('pending', 'no_policy', 'waiting_for_a_person', "
            "'evidence_not_met', 'applied')",
            name="ck_insight_lifecycle_outcome",
        ),
    )

    insight_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("agent_insight.insight_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, server_default=PENDING)
    policy_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
