"""Campaign console reads and creation: enrollment counts, the campaign
table, and turning a cohort filter into a real, enrolled campaign.
"""

from __future__ import annotations

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.enrollment import enroll_cohort
from app.db.models.campaigns import Enrollment
from app.db.models.outreach import Campaign
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor
from app.services.clients import resolve_cohort_client_ids


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


def list_campaigns(
    session: Session,
    *,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Row], str | None]:
    """Campaigns oldest-first, each carrying its own enrollment counts.

    One join instead of one summary query per row, since a real campaign
    table needs both the campaign's own fields and its counts at once.
    """
    limit = clamp_limit(limit)
    is_primary = Enrollment.is_primary_contact_row.is_(True)
    query = (
        select(
            Campaign.campaign_id,
            Campaign.name,
            Campaign.campaign_type,
            Campaign.status,
            Campaign.cohort_definition,
            Campaign.start_date,
            Campaign.end_date,
            Campaign.created_at,
            func.count(Enrollment.enrollment_id).label("total_enrolled"),
            func.count(Enrollment.enrollment_id).filter(is_primary).label("primary_count"),
        )
        .select_from(Campaign)
        .join(Enrollment, Enrollment.campaign_id == Campaign.campaign_id, isouter=True)
        .group_by(Campaign.campaign_id)
    )
    if status is not None:
        query = query.where(Campaign.status == status)
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(Campaign.campaign_id > after_id)
    query = query.order_by(Campaign.campaign_id).limit(limit + 1)

    rows = list(session.execute(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].campaign_id)
    return rows, next_cursor


def create_campaign(
    session: Session,
    *,
    name: str,
    campaign_type: str,
    cohort_filters: dict,
    start_date=None,
    end_date=None,
) -> tuple[Campaign, int]:
    """Create a campaign and enroll every client currently matching its cohort.

    cohort_filters is stored as-is on cohort_definition, the allow-listed
    feature values the cohort was selected on, so membership can be
    re-derived later; it is also used once, right here, to resolve the
    client_ids enroll_cohort actually enrolls. Returns the new campaign and
    how many client_ids matched (not how many are primary — see
    campaign_summary for the primary/suppressed split).
    """
    campaign = Campaign(
        name=name,
        campaign_type=campaign_type,
        cohort_definition=cohort_filters,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(campaign)
    session.flush()

    client_ids = resolve_cohort_client_ids(session, **cohort_filters)
    enroll_cohort(session, campaign_id=campaign.campaign_id, client_ids=client_ids)

    record_audit(
        session,
        entity_type="campaign",
        action="create",
        entity_id=str(campaign.campaign_id),
        detail={"cohort_filters": cohort_filters, "matched_count": len(client_ids)},
    )
    session.flush()
    return campaign, len(client_ids)
