from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VerifiedRateFact(Base):
    __tablename__ = "verified_rate_fact"

    fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    rate_value: Mapped[float] = mapped_column(Numeric(6, 3), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
