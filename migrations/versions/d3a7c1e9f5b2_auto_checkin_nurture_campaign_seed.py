"""auto_checkin_nurture campaign seed

Revision ID: d3a7c1e9f5b2
Revises: c8f3d7a5b1e9
Create Date: 2026-08-24 09:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3a7c1e9f5b2"
down_revision: str | Sequence[str] | None = "c8f3d7a5b1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAMPAIGN_TYPE = "auto_checkin_nurture"

# The columns as they stood when this migration shipped. Written out here,
# instead of importing the live campaign/campaign_step models, so a column
# added to those tables later never breaks this migration when history is
# replayed from scratch.
_CAMPAIGN_TABLE = sa.table(
    "campaign",
    sa.column("campaign_id"),
    sa.column("name"),
    sa.column("campaign_type"),
    sa.column("status"),
)
_CAMPAIGN_STEP_TABLE = sa.table(
    "campaign_step",
    sa.column("campaign_id"),
    sa.column("step_no"),
    sa.column("offset_days"),
    sa.column("message_angle"),
)


def upgrade() -> None:
    bind = op.get_bind()
    campaign_id = bind.execute(
        sa.insert(_CAMPAIGN_TABLE)
        .values(name="Active book: auto check-in", campaign_type=CAMPAIGN_TYPE, status="running")
        .returning(_CAMPAIGN_TABLE.c.campaign_id)
    ).scalar_one()
    bind.execute(
        sa.insert(_CAMPAIGN_STEP_TABLE).values(
            campaign_id=campaign_id,
            step_no=1,
            offset_days=0,
            message_angle="sitting_still",
        )
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM campaign_step WHERE campaign_id IN "
        f"(SELECT campaign_id FROM campaign WHERE campaign_type = '{CAMPAIGN_TYPE}')"
    )
    op.execute(f"DELETE FROM campaign WHERE campaign_type = '{CAMPAIGN_TYPE}'")
