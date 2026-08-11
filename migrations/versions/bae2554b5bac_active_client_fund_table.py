"""active_client_fund table

Revision ID: bae2554b5bac
Revises: f4a2d8c1b6e9
Create Date: 2026-08-10 13:31:28.248314

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bae2554b5bac"
down_revision: str | Sequence[str] | None = "f4a2d8c1b6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "active_client_fund",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("client_code", sa.Text(), nullable=True),
        sa.Column("balance", sa.Float(), nullable=True),
        sa.Column("n_purchases", sa.Integer(), nullable=False),
        sa.Column("n_sales", sa.Integer(), nullable=False),
        sa.Column("last_purchase", sa.Date(), nullable=True),
        sa.Column("last_sale", sa.Date(), nullable=True),
        sa.Column(
            "purchases_censored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "redemption_history_blind",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("computed_at", sa.Text(), nullable=True),
        sa.Column("rhythm_days", sa.Float(), nullable=True),
        sa.Column("avg_ticket", sa.Float(), nullable=True),
        sa.Column("max_ticket", sa.Float(), nullable=True),
        sa.Column("last_ticket", sa.Float(), nullable=True),
        sa.Column("ticket_trend", sa.Float(), nullable=True),
        sa.Column("largest_real_sale", sa.Float(), nullable=True),
        sa.Column("fee_runway_months", sa.Float(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id"),
    )
    # No grant to the model-facing role: this table is not model-facing at
    # all, and later carries figures the same way client_fund does.


def downgrade() -> None:
    op.drop_table("active_client_fund")
