"""How much freedom each action has right now."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The level used when no row has been written for an action yet.
DEFAULT_PERMISSION = "suggest_only"


class AgentPermission(Base):
    """One setting: what this action may do on its own, for these clients."""

    __tablename__ = "agent_permission"
    __table_args__ = (
        Index(
            "uq_agent_permission_action_tier_band_live",
            "action_code",
            "priority_tier",
            "risk_band",
            "group_name",
            "fa_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "permission IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_permission_permission",
        ),
        CheckConstraint(
            "max_clients_per_day IS NULL OR max_clients_per_day >= 0",
            name="ck_agent_permission_max_clients_positive",
        ),
        CheckConstraint(
            "(action_code IS NULL) <> (group_name IS NULL)",
            name="ck_agent_permission_action_or_group",
        ),
        CheckConstraint(
            "action_code IS NULL OR permission IS NOT NULL",
            name="ck_agent_permission_action_has_level",
        ),
        CheckConstraint(
            "content_mix IS NULL OR content_mix IN "
            "('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
            name="ck_agent_permission_content_mix",
        ),
        CheckConstraint(
            "(min_balance IS NULL OR min_balance >= 0) AND "
            "(max_balance IS NULL OR min_balance IS NULL OR max_balance > min_balance)",
            name="ck_agent_permission_balance_range",
        ),
        CheckConstraint(
            "max_money_kes IS NULL OR max_money_kes > 0",
            name="ck_agent_permission_max_money_positive",
        ),
    )

    permission_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # A row is for one action, or for one client group of the active agent.
    action_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    group_name: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # Group rows only: empty means every adviser.
    fa_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Empty means the row covers every tier.
    priority_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Empty means the row covers every risk band.
    risk_band: Mapped[str | None] = mapped_column(Text, nullable=True)
    # On a group row, an empty value follows the all advisers row, then the default.
    permission: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Empty means no daily cap on how many clients the action may reach.
    # On a group row, 0 means no cap and empty follows the wider row.
    max_clients_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    balance_filter: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    min_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_mix: Mapped[str | None] = mapped_column(Text, nullable=True)
    guide_doc_ids: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    use_rates: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    no_repeat: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    paused: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Empty means no daily cap on the money the action may touch.
    max_money_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
