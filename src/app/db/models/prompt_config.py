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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CONFIGURATION_STATUSES = ("draft", "published", "archived")


class ActiveConfiguration(Base):
    __tablename__ = "active_configuration"
    __table_args__ = (
        UniqueConstraint(
            "component_type", "component_key", name="uq_active_configuration_component"
        ),
    )

    config_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    component_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    component_key: Mapped[str] = mapped_column(Text, nullable=False)
    active_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VoiceContract(Base):
    __tablename__ = "voice_contract"
    __table_args__ = (
        UniqueConstraint("version", name="uq_voice_contract_version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_voice_contract_status"
        ),
    )

    voice_contract_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str | None] = mapped_column(Text, nullable=True)
    writing_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    structure: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    length_guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    readability_guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SafetyPolicy(Base):
    __tablename__ = "safety_policy"
    __table_args__ = (
        UniqueConstraint("version", name="uq_safety_policy_version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_safety_policy_status"
        ),
    )

    safety_policy_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    banned_words: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    banned_phrases: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    campaign_prohibitions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    claim_restrictions: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutputPolicy(Base):
    __tablename__ = "output_policy"
    __table_args__ = (
        UniqueConstraint("version", name="uq_output_policy_version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_output_policy_status"
        ),
    )

    output_policy_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    output_schema_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    placeholder_rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    formatting_restrictions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactEligibilityRule(Base):
    __tablename__ = "fact_eligibility_rule"
    __table_args__ = (
        UniqueConstraint(
            "policy_version", "angle", "fact_field", name="uq_fact_eligibility_rule_scope"
        ),
        CheckConstraint(
            "exposure_mode IN ('direct', 'placeholder', 'conditional', 'prohibited')",
            name="ck_fact_eligibility_rule_exposure_mode",
        ),
    )

    rule_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_field: Mapped[str] = mapped_column(Text, nullable=False)
    exposure_mode: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PersonalizationPolicy(Base):
    __tablename__ = "personalization_policy"
    __table_args__ = (
        UniqueConstraint("version", name="uq_personalization_policy_version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_personalization_policy_status",
        ),
    )

    personalization_policy_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
