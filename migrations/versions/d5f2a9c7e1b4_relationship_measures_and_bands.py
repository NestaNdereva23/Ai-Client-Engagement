"""relationship measures on client_fund and behavioural bands on client_features

Revision ID: d5f2a9c7e1b4
Revises: c4e8b1f6a2d3
Create Date: 2026-08-03 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f2a9c7e1b4"
down_revision: str | Sequence[str] | None = "c4e8b1f6a2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nullable: a measure the transactions cannot support stays absent rather than
# being filled with a zero that would read as a real value.
_MEASURES = [
    ("avg_ticket", sa.Float()),
    ("max_ticket", sa.Float()),
    ("rhythm_days", sa.Float()),
    ("first_purchase", sa.Date()),
    ("active_window_days", sa.Integer()),
    ("ticket_trend", sa.Float()),
    ("first_sale", sa.Date()),
    ("drawdown_days", sa.Integer()),
    ("hold_days", sa.Integer()),
    ("exit_type", sa.Text()),
]

# Added alongside the existing buckets rather than replacing them. A rule matches
# on the exact string, so changing a value set under its old name would stop
# every rule using it from matching, and route those clients to the catch-all
# without failing anything.
_BANDS = [
    ("recency_band", sa.Text(), "Unknown"),
    ("value_band", sa.Text(), "Low"),
    ("cadence_band", sa.Text(), "None"),
    ("hold_band", sa.Text(), "Unknown"),
    ("purchase_depth", sa.Text(), "none"),
    ("trend_band", sa.Text(), "unknown"),
    ("exit_reason", sa.Text(), "unknown"),
    ("fund_type", sa.Text(), "other"),
    ("in_wave", sa.Boolean(), sa.text("false")),
    ("has_depth", sa.Boolean(), sa.text("false")),
    ("staged_exit", sa.Boolean(), sa.text("false")),
    ("stale_contact", sa.Boolean(), sa.text("false")),
    ("holds_other_funds", sa.Boolean(), sa.text("false")),
]


def upgrade() -> None:
    for name, type_ in _MEASURES:
        op.add_column("client_fund", sa.Column(name, type_, nullable=True))

    op.add_column(
        "client_features", sa.Column("n_funds", sa.Integer(), server_default="1", nullable=False)
    )
    for name, type_, default in _BANDS:
        op.add_column(
            "client_features", sa.Column(name, type_, server_default=default, nullable=False)
        )


def downgrade() -> None:
    for name, _type, _default in reversed(_BANDS):
        op.drop_column("client_features", name)
    op.drop_column("client_features", "n_funds")

    for name, _type in reversed(_MEASURES):
        op.drop_column("client_fund", name)
