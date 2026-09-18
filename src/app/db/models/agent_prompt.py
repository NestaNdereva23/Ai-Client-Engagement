from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentPrompt(Base):
    __tablename__ = "agent_prompt"
    __table_args__ = (
        UniqueConstraint("version", "prompt_name", name="uq_agent_prompt_version_prompt_name"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_agent_prompt_status"
        ),
    )

    prompt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    prompt_name: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
