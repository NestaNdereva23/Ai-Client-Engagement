"""auto_checkin_nurture campaign seed

Revision ID: d3a7c1e9f5b2
Revises: c8f3d7a5b1e9
Create Date: 2026-08-24 09:15:00.000000
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from app.db.models.campaigns import CampaignStep
from app.db.models.outreach import Campaign

revision: str = "d3a7c1e9f5b2"
down_revision: str | Sequence[str] | None = "c8f3d7a5b1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAMPAIGN_TYPE = "auto_checkin_nurture"


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    campaign = Campaign(
        name="Active book: auto check-in",
        campaign_type=CAMPAIGN_TYPE,
        status="running",
    )
    session.add(campaign)
    session.flush()
    session.add(
        CampaignStep(
            campaign_id=campaign.campaign_id,
            step_no=1,
            offset_days=0,
            message_angle="sitting_still",
        )
    )
    session.flush()


def downgrade() -> None:
    op.execute(
        f"DELETE FROM campaign_step WHERE campaign_id IN "
        f"(SELECT campaign_id FROM campaign WHERE campaign_type = '{CAMPAIGN_TYPE}')"
    )
    op.execute(f"DELETE FROM campaign WHERE campaign_type = '{CAMPAIGN_TYPE}'")
