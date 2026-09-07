"""How much freedom each action has right now."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The level used when no row has been written for an action yet.
DEFAULT_PERMISSION = "suggest_only"


class AgentPermission(Base):
    """One setting: what this action may do on its own, for these clients."""

    __tablename__ = "agent_permission"
    __table_args__ = (
        UniqueConstraint(
            "action_code",
            "priority_tier",
            "risk_band",
            name="uq_agent_permission_action_tier_band",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "permission IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_permission_permission",
        ),
        CheckConstraint(
            "max_clients_per_day IS NULL OR max_clients_per_day > 0",
            name="ck_agent_permission_max_clients_positive",
        ),
        CheckConstraint(
            "max_money_kes IS NULL OR max_money_kes > 0",
            name="ck_agent_permission_max_money_positive",
        ),
    )

    permission_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_code: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Empty means the row covers every tier.
    priority_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Empty means the row covers every risk band.
    risk_band: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    # Empty means no daily cap on how many clients the action may reach.
    max_clients_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Empty means no daily cap on the money the action may touch.
    max_money_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
