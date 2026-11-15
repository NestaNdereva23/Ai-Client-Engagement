from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e3a1c9d2f4"
down_revision: str | Sequence[str] | None = "10c1239fe4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAST_DEPOSITORS = [
    "inactive:low_depositors",
    "inactive:one_time_withdrawers",
    "inactive:gradual_withdrawers",
    "inactive:frequent_withdrawers",
]

AUDIENCE_BY_BASE = {
    "send_learning_note": ["all"],
    "start_win_back": ["inactive"],
    "follow_up_when_no_one_called": ["inactive"],
    "first_deposit_welcome": ["inactive:hot_leads"],
    "onboarding_retry": ["inactive:hot_leads"],
    "check_in_rising_risk": ["active"],
    "fee_pressure_encourage_active": ["active"],
}


def upgrade() -> None:
    op.add_column(
        "message_angle_catalog",
        sa.Column(
            "audience",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    table = sa.table(
        "message_angle_catalog",
        sa.column("angle", sa.Text()),
        sa.column("audience", postgresql.ARRAY(sa.Text())),
    )
    base = sa.func.split_part(table.c.angle, "__", 1)
    for code, audience in AUDIENCE_BY_BASE.items():
        op.execute(table.update().where(base == code).values(audience=audience))
    op.execute(
        table.update().where(base.notin_(list(AUDIENCE_BY_BASE))).values(audience=PAST_DEPOSITORS)
    )


def downgrade() -> None:
    op.drop_column("message_angle_catalog", "audience")
