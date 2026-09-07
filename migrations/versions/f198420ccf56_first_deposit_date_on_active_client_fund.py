"""first deposit date on active client fund

Revision ID: f198420ccf56
Revises: 6c38ff3ab9d8
Create Date: 2026-09-07 11:30:22.318877

The dormant book already carries this as client_fund.first_purchase, filled
by transform/load.py. This adds the active-book match so the nightly
transform can find recently signed up clients without scanning
active_transaction every time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f198420ccf56"
down_revision: str | Sequence[str] | None = "6c38ff3ab9d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("active_client_fund", sa.Column("first_deposit_date", sa.Date(), nullable=True))
    op.execute(
        """
        UPDATE active_client_fund AS acf
        SET first_deposit_date = earliest.first_deposit_date
        FROM (
            SELECT client_id, unit_fund_id, MIN(txn_date) AS first_deposit_date
            FROM active_transaction
            WHERE txn_type = 'purchase' AND txn_date IS NOT NULL
            GROUP BY client_id, unit_fund_id
        ) AS earliest
        WHERE acf.client_id = earliest.client_id
          AND acf.unit_fund_id = earliest.unit_fund_id
        """
    )


def downgrade() -> None:
    op.drop_column("active_client_fund", "first_deposit_date")
