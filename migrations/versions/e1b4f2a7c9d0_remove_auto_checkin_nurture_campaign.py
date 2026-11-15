"""remove auto_checkin_nurture campaign seed

Revision ID: e1b4f2a7c9d0
Revises: 00c8b7dc51e7
Create Date: 2026-10-22 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1b4f2a7c9d0"
down_revision: str | Sequence[str] | None = "00c8b7dc51e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAMPAIGN_TYPE = "auto_checkin_nurture"


def upgrade() -> None:
    # The active-book auto check-in feature is parked and enrollment is a
    # no-op, so this seeded campaign never gets clients. It only clutters the
    # campaign list, where it reads as an "Active book" row among dormant
    # campaigns. Remove it. Any outreach_message FK would stop the delete,
    # which is the right outcome: that would mean real history to keep.
    op.execute(
        f"DELETE FROM campaign_step WHERE campaign_id IN "
        f"(SELECT campaign_id FROM campaign WHERE campaign_type = '{CAMPAIGN_TYPE}')"
    )
    op.execute(f"DELETE FROM campaign WHERE campaign_type = '{CAMPAIGN_TYPE}'")


def downgrade() -> None:
    bind = op.get_bind()
    campaign = sa.table(
        "campaign",
        sa.column("campaign_id"),
        sa.column("name"),
        sa.column("campaign_type"),
        sa.column("status"),
    )
    campaign_step = sa.table(
        "campaign_step",
        sa.column("campaign_id"),
        sa.column("step_no"),
        sa.column("offset_days"),
        sa.column("message_angle"),
    )
    campaign_id = bind.execute(
        sa.insert(campaign)
        .values(name="Active book: auto check-in", campaign_type=CAMPAIGN_TYPE, status="running")
        .returning(campaign.c.campaign_id)
    ).scalar_one()
    bind.execute(
        sa.insert(campaign_step).values(
            campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="sitting_still"
        )
    )
