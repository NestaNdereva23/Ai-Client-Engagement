from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models.outreach import AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign
from app.db.session import SessionLocal

ACTIVE_SETTING_NAMES = {
    "delivery_mode": "active_delivery_mode",
    "live_sms_to_test_list": "active_live_sms_to_test_list",
    "dispatch_direct_send": "active_dispatch_direct_send",
    "dispatch_direct_send_max_batch": "active_dispatch_direct_send_max_batch",
    "live_campaign_max_clients": "active_live_campaign_max_clients",
    "test_campaign_max_clients": "active_test_campaign_max_clients",
    "test_client_first_name": "active_test_client_first_name",
    "test_subject_prefix": "active_test_subject_prefix",
}


def active_settings(settings: Settings | None = None) -> Settings:
    settings = settings or get_settings()
    return settings.model_copy(
        update={name: getattr(settings, own) for name, own in ACTIVE_SETTING_NAMES.items()}
    )


def settings_for_type(campaign_type: str | None, settings: Settings | None = None) -> Settings:
    if campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE:
        return active_settings(settings)
    return settings or get_settings()


def settings_for_campaign(
    session: Session, campaign_id: int, settings: Settings | None = None
) -> Settings:
    campaign_type = session.scalar(
        select(Campaign.campaign_type).where(Campaign.campaign_id == campaign_id)
    )
    return settings_for_type(campaign_type, settings)


def settings_for_message(campaign_id: int, settings: Settings | None = None) -> Settings:
    with SessionLocal() as session:
        return settings_for_campaign(session, campaign_id, settings)


def enrollment_cap(settings: Settings) -> int:
    if settings.delivery_mode == "test":
        return settings.test_campaign_max_clients
    return settings.live_campaign_max_clients
