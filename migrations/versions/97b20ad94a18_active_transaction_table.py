"""active transaction table

Revision ID: 97b20ad94a18
Revises: 11e9528e2fb5
Create Date: 2026-08-14 08:41:03.552112

One purchase or sale observed for an active-book client-fund, upserted on
txn_id every transform run so a row that ages out of the feed's own
"last 5 purchases" / "last 2 sales" window on a later pull stays visible
here instead of disappearing -- this table accumulates across nightly
runs, the feed itself never does. Backs the active-client profile page's
transaction ledger; see transform/active_load.py for the write path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "97b20ad94a18"
down_revision: str | Sequence[str] | None = "11e9528e2fb5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "active_transaction",
        sa.Column("txn_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("txn_type", sa.Text(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("fund_short_name", sa.Text(), nullable=True),
        sa.Column("txn_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Float(), server_default="0", nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=True),
        sa.Column("fees_incurred", sa.Float(), nullable=True),
        sa.Column("sale_type", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("txn_id"),
    )
    op.create_index(
        "ix_active_transaction_client_fund",
        "active_transaction",
        ["client_id", "unit_fund_id", "txn_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_active_transaction_client_fund", table_name="active_transaction")
    op.drop_table("active_transaction")
