"""client_fund relationship table and clients.n_funds

Revision ID: c4e8b1f6a2d3
Revises: 12e1e721c5d2
Create Date: 2026-08-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8b1f6a2d3"
down_revision: str | Sequence[str] | None = "12e1e721c5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_fund",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_fund_id", sa.BigInteger(), nullable=False),
        sa.Column("client_code", sa.Text(), nullable=True),
        sa.Column("balance", sa.Float(), nullable=True),
        sa.Column("n_purchases", sa.Integer(), nullable=False),
        sa.Column("n_sales", sa.Integer(), nullable=False),
        sa.Column("last_purchase", sa.Date(), nullable=True),
        sa.Column("last_sale", sa.Date(), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("days_cold", sa.Integer(), nullable=True),
        sa.Column("observed_volume", sa.Float(), server_default="0", nullable=False),
        sa.Column(
            "purchases_censored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "history_censored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "is_primary_contact_row", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("computed_at", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.ForeignKeyConstraint(["unit_fund_id"], ["funds.unit_fund_id"]),
        sa.PrimaryKeyConstraint("client_id", "unit_fund_id"),
    )
    # Enrollment and campaign selection read this by client to find the row to
    # contact, which the composite key alone does not serve.
    op.create_index(
        "ix_client_fund_primary",
        "client_fund",
        ["client_id"],
        postgresql_where=sa.text("is_primary_contact_row"),
    )
    op.add_column("clients", sa.Column("n_funds", sa.Integer(), server_default="1", nullable=False))
    # No grant to the model-facing role: it carries exact amounts and dates, and
    # that role reads only allow-listed views. Deny by default, as with the other
    # tables holding real figures.


def downgrade() -> None:
    op.drop_column("clients", "n_funds")
    op.drop_index("ix_client_fund_primary", table_name="client_fund")
    op.drop_table("client_fund")
