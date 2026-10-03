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
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ActionResult(Base):
    __tablename__ = "action_result"
    __table_args__ = (
        UniqueConstraint("message_id", "window_days", name="uq_action_result_message_window"),
        CheckConstraint("window_days > 0", name="ck_action_result_window_days_positive"),
        CheckConstraint(
            "deposit_amount_kes >= 0", name="ck_action_result_deposit_amount_not_negative"
        ),
    )

    result_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_proposal.proposal_id"), nullable=False, index=True
    )
    insight_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_insight.insight_id"), nullable=True, index=True
    )
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("outreach_message.message_id"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    opened: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    replied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    bounced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    reviewer_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    deposited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    deposit_amount_kes: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
