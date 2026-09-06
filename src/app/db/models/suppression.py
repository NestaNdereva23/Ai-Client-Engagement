"""The compliance suppression list: client_ids who must never be contacted."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Suppression(Base):
    __tablename__ = "suppression"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
