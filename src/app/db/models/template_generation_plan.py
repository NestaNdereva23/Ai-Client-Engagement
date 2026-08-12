"""One row per draft_templates_for_campaign call: the estimate, the limit,
and what actually happened.

Without this, the only record of "we estimated 87 and asked for 40" is an
API response nobody kept, and a page refresh has nothing left to show. The
policy columns are a copy, not a foreign key, so an old plan stays
explainable against whatever was in force when it ran even if the campaign's
own policy or the system default changes later.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TemplateGenerationPlan(Base):
    """One drafting call's estimated/limit/actual numbers, and the policy in force."""

    __tablename__ = "template_generation_plan"
    __table_args__ = (
        CheckConstraint("estimated_templates >= 0", name="ck_tgp_estimated_nonneg"),
        CheckConstraint(
            "effective_limit IS NULL OR effective_limit >= 0", name="ck_tgp_effective_limit_nonneg"
        ),
        CheckConstraint("drafted_count >= 0", name="ck_tgp_drafted_nonneg"),
        CheckConstraint("skipped_existing >= 0", name="ck_tgp_skipped_nonneg"),
        CheckConstraint("failed_guardrails >= 0", name="ck_tgp_failed_nonneg"),
        CheckConstraint("policy_source IN ('campaign', 'default')", name="ck_tgp_policy_source"),
    )

    plan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), nullable=False, index=True
    )
    estimated_templates: Mapped[int] = mapped_column(Integer, nullable=False)
    # Null means no limit applied, same reading as everywhere else in the
    # policy chain (campaign_template_policy, template_policy_config_version).
    effective_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drafted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped_existing: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_guardrails: Mapped[int] = mapped_column(Integer, nullable=False)
    # "campaign" or "default" -- where the limit above came from.
    policy_source: Mapped[str] = mapped_column(Text, nullable=False)
    policy_max_templates: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_max_templates_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
