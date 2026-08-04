"""Campaign console reads: enrollment counts, including primary-row suppression."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.campaigns import Enrollment
from app.db.models.outreach import Campaign


class CampaignNotFound(Exception):
    """No campaign exists with the given id."""


def campaign_summary(session: Session, campaign_id: int) -> dict[str, int]:
    """Enrollment counts for one campaign: total, primary, and suppressed rows.

    A suppressed row is enrolled but never sends: is_primary_contact_row is
    false because another client_id for the same person already claimed it.
    Raises CampaignNotFound when campaign_id names no campaign.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    total = session.execute(
        select(func.count()).select_from(Enrollment).where(Enrollment.campaign_id == campaign_id)
    ).scalar_one()
    primary = session.execute(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.campaign_id == campaign_id,
            Enrollment.is_primary_contact_row.is_(True),
        )
    ).scalar_one()

    return {
        "total_enrolled": total,
        "primary_count": primary,
        "suppressed_count": total - primary,
    }
