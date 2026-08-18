"""email_sent interaction type and digest tiering

Revision ID: a4c8e1f6b9d3
Revises: ece296116d34
Create Date: 2026-08-18 10:00:00.000000

Lets an FA manager log "emailed this client" alongside a call, snooze, or
dismiss, and gives digest/build.py what it needs to rank a touched
client-fund below an untouched one: the risk band in force at the moment
each interaction was logged, and a persisted flag for whether a line ended
up deprioritized by that rule.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c8e1f6b9d3"
down_revision: str | Sequence[str] | None = "ece296116d34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "active_client_interaction",
        sa.Column("risk_band_at_interaction", sa.Text(), nullable=True),
    )
    op.drop_constraint(
        "ck_active_client_interaction_type", "active_client_interaction", type_="check"
    )
    op.create_check_constraint(
        "ck_active_client_interaction_type",
        "active_client_interaction",
        "type IN ('call_logged', 'snoozed', 'dismissed', 'email_sent')",
    )

    op.add_column(
        "digest_line",
        sa.Column("deprioritized", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("digest_line", "deprioritized")

    op.drop_constraint(
        "ck_active_client_interaction_type", "active_client_interaction", type_="check"
    )
    op.create_check_constraint(
        "ck_active_client_interaction_type",
        "active_client_interaction",
        "type IN ('call_logged', 'snoozed', 'dismissed')",
    )
    op.drop_column("active_client_interaction", "risk_band_at_interaction")
