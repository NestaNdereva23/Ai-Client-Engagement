"""The versioned business-rule store.

Each rule belongs to a numbered version with its own validity window. A shipped
version is never mutated; edits ship as a new version. match is a mapping of
allow-listed feature name to accepted values; an absent feature is a wildcard.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BusinessRule(Base):
    __tablename__ = "business_rules"
    __table_args__ = (
        UniqueConstraint("version", "priority", name="uq_business_rules_version_priority"),
    )

    rule_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    match: Mapped[dict] = mapped_column(JSONB, nullable=False)
    message_angle: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(Text, nullable=False)
    priority_tier: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_variant: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClientMessageIndicators(Base):
    """The resolved outreach angle for one client, with the winning rule recorded.

    One row per client, upserted each resolution run. rule_id and rule_version
    make the outcome traceable back to the rule that produced it.
    """

    __tablename__ = "client_message_indicators"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.client_id"), primary_key=True, autoincrement=False
    )
    message_angle: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(Text, nullable=False)
    priority_tier: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_variant: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("business_rules.rule_id"), nullable=True
    )
    rule_name: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
