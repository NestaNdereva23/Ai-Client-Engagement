from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.campaigns.completion import campaign_progress, complete_finished_campaigns
from app.config import get_settings
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal

FUND_ID = 99670
FINISHED = 996701
STILL_GOING = 996702
ALL_CLIENTS = (FINISHED, STILL_GOING)
CAMPAIGN_NAME = "Campaign completion test campaign"
LAST_SENT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _purge(session) -> None:
    campaign_ids = session.scalars(
        select(Campaign.campaign_id).where(Campaign.name == CAMPAIGN_NAME)
    ).all() or [0]
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.campaign_id.in_(campaign_ids))
    ).all() or [0]
    session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
    session.execute(delete(Enrollment).where(Enrollment.enrollment_id.in_(enrollment_ids)))
    session.execute(delete(CampaignStep).where(CampaignStep.campaign_id.in_(campaign_ids)))
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "campaign",
            AuditLog.entity_id.in_([str(c) for c in campaign_ids]),
        )
    )
    session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.commit()


@pytest.fixture
def campaign():
    with SessionLocal() as session:
        _purge(session)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Completion test fund"))
        session.commit()
        session.add_all(
            Clients(client_id=c, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0)
            for c in ALL_CLIENTS
        )
        row = Campaign(name=CAMPAIGN_NAME, campaign_type="dormant_reengagement", status="running")
        session.add(row)
        session.commit()
        session.add_all(
            [
                CampaignStep(campaign_id=row.campaign_id, step_no=1, offset_days=0),
                CampaignStep(campaign_id=row.campaign_id, step_no=2, offset_days=1),
            ]
        )
        done = Enrollment(
            campaign_id=row.campaign_id, client_id=FINISHED, current_step=2, status="completed"
        )
        going = Enrollment(
            campaign_id=row.campaign_id,
            client_id=STILL_GOING,
            current_step=1,
            status="in_progress",
            next_due_at=LAST_SENT,
        )
        session.add_all([done, going])
        session.commit()
        session.add(TouchLog(enrollment_id=done.enrollment_id, step_no=2, sent_at=LAST_SENT))
        session.commit()
        yield row.campaign_id, going.enrollment_id
        _purge(session)


def test_campaign_completes_only_after_every_client_and_the_cooldown(campaign):
    campaign_id, going_id = campaign
    cooldown = timedelta(days=get_settings().campaign_cooldown_days)
    with SessionLocal() as session:
        progress = campaign_progress(session, session.get(Campaign, campaign_id))
        assert progress["clients"] == {"waiting": 0, "in_progress": 1, "finished": 1, "stopped": 0}
        assert progress["expected_complete_at"] == LAST_SENT + cooldown
        assert complete_finished_campaigns(session, LAST_SENT + timedelta(days=90)) == []

        session.get(Enrollment, going_id).status = "completed"
        session.commit()
        assert (
            complete_finished_campaigns(session, LAST_SENT + cooldown - timedelta(minutes=1)) == []
        )
        assert complete_finished_campaigns(session, LAST_SENT + cooldown) == [campaign_id]
        session.commit()
        assert session.get(Campaign, campaign_id).status == "completed"
