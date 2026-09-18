from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TestRecipient(Base):
    __tablename__ = "test_recipient"
    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL", name="ck_test_recipient_has_contact"
        ),
    )
    __test__ = False

    recipient_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
