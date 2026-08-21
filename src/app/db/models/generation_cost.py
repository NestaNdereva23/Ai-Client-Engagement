"""How much one AI generation call costs, with retrieval-augmented context.

generation_cost_config_version holds the RAG-enabled per-generation rate,
versioned exactly like risk_config_version and template_policy_config_version:
a shipped version is never edited, so a cost estimate given today stays
explainable against the rate that was live when it ran. One generation call
costs the same whether it drafts one client's message or one bucket's
template -- both make exactly one call through the same generation graph --
so a single rate covers both drafting modes; see app.campaigns.generation_cost.

Each supported model keeps its own version sequence: "version 1" means
something different for claude-haiku-4-5-20251001 than it does for
claude-sonnet-5, so the two rows can be superseded independently.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GenerationCostConfigVersion(Base):
    """One immutable per-generation cost rate, RAG-enabled generation only.

    Selection mirrors risk_config_version and template_policy_config_version:
    for a given model, the active version is the one with the latest
    valid_from that has started and not ended. cost_per_generation_usd and
    _kes are the "with RAG, single generation" rate on the standard
    (non-batch) API -- see Generation Cost Estimate - RAG versus No RAG.docx
    for how the Claude Haiku 4.5 rate was measured; the other models' rates
    are derived from Anthropic's published per-token pricing applied to that
    same measured token profile, pending their own measured traces.
    """

    __tablename__ = "generation_cost_config_version"
    __table_args__ = (
        CheckConstraint("cost_per_generation_usd > 0", name="ck_gccv_usd_positive"),
        CheckConstraint("cost_per_generation_kes > 0", name="ck_gccv_kes_positive"),
        UniqueConstraint("model", "version", name="uq_gccv_model_version"),
        Index("ix_gccv_model_valid_from", "model", "valid_from"),
    )

    config_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    cost_per_generation_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cost_per_generation_kes: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
