from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from app.db.models.campaigns import Enrollment
from app.db.models.fa_assignment import FaAssignment
from app.db.models.outreach import AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign, OutreachMessage


def _campaigns_of_advisor(fa_id: str):
    return (
        select(Enrollment.campaign_id)
        .join(FaAssignment, FaAssignment.client_id == Enrollment.client_id)
        .where(FaAssignment.fa_id == fa_id)
    )


def _other_campaigns():
    return select(Campaign.campaign_id).where(
        Campaign.campaign_type != AGENT_PROPOSAL_CAMPAIGN_TYPE
    )


def campaign_visible_to(fa_id: str) -> ColumnElement[bool]:
    return or_(
        Campaign.campaign_type != AGENT_PROPOSAL_CAMPAIGN_TYPE,
        Campaign.campaign_id.in_(_campaigns_of_advisor(fa_id)),
    )


def message_visible_to(fa_id: str) -> ColumnElement[bool]:
    return or_(
        OutreachMessage.campaign_id.in_(_other_campaigns()),
        OutreachMessage.campaign_id.in_(_campaigns_of_advisor(fa_id)),
    )


def campaign_is_visible(session: Session, campaign_id: int, fa_id: str) -> bool:
    return (
        session.scalar(
            select(Campaign.campaign_id).where(
                Campaign.campaign_id == campaign_id, campaign_visible_to(fa_id)
            )
        )
        is not None
    )


def message_is_visible(session: Session, message_id: str, fa_id: str) -> bool:
    return (
        session.scalar(
            select(OutreachMessage.message_id).where(
                OutreachMessage.message_id == message_id, message_visible_to(fa_id)
            )
        )
        is not None
    )
