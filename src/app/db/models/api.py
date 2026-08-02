"""Shared API infrastructure that isn't owned by any one domain.

idempotency_keys stores the first response for a state-changing request, keyed
by the caller's Idempotency-Key header plus the method and path it was sent
to (the same key reused against a different endpoint is a different request).
A replay of that exact combination returns the stored response instead of
re-executing the handler.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, PrimaryKeyConstraint, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (PrimaryKeyConstraint("idempotency_key", "method", "path"),)

    idempotency_key: Mapped[str] = mapped_column(Text, autoincrement=False)
    method: Mapped[str] = mapped_column(Text, autoincrement=False)
    path: Mapped[str] = mapped_column(Text, autoincrement=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
