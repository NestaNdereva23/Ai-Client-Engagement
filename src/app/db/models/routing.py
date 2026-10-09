from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ClientSide(Base):
    __tablename__ = "client_side"
    __table_args__ = (
        CheckConstraint("side in ('active', 'inactive')", name="ck_client_side_side"),
    )

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    threshold_kes: Mapped[float] = mapped_column(Float, nullable=False)
    source_feed: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
