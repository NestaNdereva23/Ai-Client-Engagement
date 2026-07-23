"""normalized funds clients transactions tables

Revision ID: 29c0903cd101
Revises: d41c7a9e2b18
Create Date: 2026-07-23 09:38:24.776509

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "29c0903cd101"
down_revision: str | Sequence[str] | None = "d41c7a9e2b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "funds",
        sa.Column("unit_fund_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("unit_fund_name", sa.Text(), nullable=False),
        sa.Column("inactive_client_count", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("unit_fund_id"),
    )

    op.create_table(
        "clients",
        sa.Column("client_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("client_code", sa.Text(), nullable=True),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.Float(), nullable=True),
        sa.Column("n_purchases_returned", sa.Integer(), nullable=False),
        sa.Column("n_sales_returned", sa.Integer(), nullable=False),
        sa.Column("last_purchase_date", sa.Date(), nullable=True),
        sa.Column("last_sale_date", sa.Date(), nullable=True),
        sa.Column("total_purchase_amount", sa.Float(), server_default="0", nullable=False),
        sa.Column("total_sale_amount", sa.Float(), server_default="0", nullable=False),
        sa.Column("last_activity_date", sa.Date(), nullable=True),
        sa.Column("days_since_last_activity", sa.Integer(), nullable=True),
        sa.Column("net_flow", sa.Float(), server_default="0", nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["unit_fund_id"], ["funds.unit_fund_id"]),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_index(op.f("ix_clients_unit_fund_id"), "clients", ["unit_fund_id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("txn_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("txn_type", sa.String(length=16), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=True),
        sa.Column("fund_short_name", sa.Text(), nullable=True),
        sa.Column("txn_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Float(), server_default="0", nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=True),
        sa.Column("fees_incurred", sa.Float(), nullable=True),
        sa.Column("sale_type", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.PrimaryKeyConstraint("txn_id"),
    )
    op.create_index(op.f("ix_transactions_client_id"), "transactions", ["client_id"], unique=False)
    op.create_index(
        op.f("ix_transactions_unit_fund_id"), "transactions", ["unit_fund_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_transactions_unit_fund_id"), table_name="transactions")
    op.drop_index(op.f("ix_transactions_client_id"), table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(op.f("ix_clients_unit_fund_id"), table_name="clients")
    op.drop_table("clients")
    op.drop_table("funds")
