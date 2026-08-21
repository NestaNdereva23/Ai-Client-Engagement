"""briefing_narrative: one stored AI narration per client-fund relationship.

Drafting a narration takes long enough on a locally hosted model that an FA
working a morning queue will not wait for it. The nightly run pre-drafts one
for every client the digest actually surfaces, so opening those is instant;
every other client is narrated on request and the result kept here, so the
second look is instant too.

facts_hash is what makes a stored narration safe to serve. It fingerprints
the exact RiskFactBlock the text was written from. When tonight's facts
hash differently the stored text is stale, and a stale narration is worse
than the deterministic briefing, so the read path ignores it. No clock and
no expiry window are involved.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BriefingNarrative(Base):
    """The most recent accepted narration for one client-fund relationship.

    One row per relationship, overwritten in place: only the narration that
    matches the current facts is ever of use, so there is no history to
    keep here. A fallback is never stored, because there is nothing to
    store, only the deterministic text the caller already has.
    """

    __tablename__ = "briefing_narrative"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    narrative_text: Mapped[str] = mapped_column(Text, nullable=False)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_block_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
