"""How many templates a campaign's drafting call is allowed to produce.

campaign_template_policy holds the one, optional override a campaign manager
set for one campaign. template_policy_config_version holds the system
default a campaign with no override inherits, versioned exactly like
risk_config_version: a shipped version is never edited, so an old drafting
call can still be explained against the default that was live when it ran.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CampaignTemplatePolicy(Base):
    """One campaign's own template generation limit, if it has set one.

    A campaign with no row here is not blocked; it inherits whatever
    template_policy_config_version is active. max_templates and
    max_templates_pct may both be set, in which case the smaller of the two
    resulting caps applies (see campaigns.template_policy.effective_limit).
    """

    __tablename__ = "campaign_template_policy"
    __table_args__ = (
        CheckConstraint("max_templates IS NULL OR max_templates > 0", name="ck_ctp_max_positive"),
        CheckConstraint(
            "max_templates_pct IS NULL OR (max_templates_pct >= 1 AND max_templates_pct <= 100)",
            name="ck_ctp_pct_range",
        ),
    )

    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaign.campaign_id"), primary_key=True, autoincrement=False
    )
    max_templates: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_templates_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class TemplatePolicyConfigVersion(Base):
    """One immutable set of default template generation limits.

    Selection mirrors risk_config_version and business rules: the active
    version is the one with the latest valid_from that has started and not
    ended. Both limit fields may be null, meaning the default is no cap.
    """

    __tablename__ = "template_policy_config_version"
    __table_args__ = (
        CheckConstraint(
            "default_max_templates IS NULL OR default_max_templates > 0",
            name="ck_tpcv_max_positive",
        ),
        CheckConstraint(
            "default_max_templates_pct IS NULL "
            "OR (default_max_templates_pct >= 1 AND default_max_templates_pct <= 100)",
            name="ck_tpcv_pct_range",
        ),
    )

    config_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    default_max_templates: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_max_templates_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
