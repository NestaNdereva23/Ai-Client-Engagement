"""newly_dormant feature column and llm_client_context view

Revision ID: 11c3e0f1020d
Revises: b3ef3bf2c1ea
Create Date: 2026-08-11 22:30:15.908285

A client who went quiet in the last 90 days is warm and reachable, not a
historical exit; EDA4 found this is over a third of the whole dormant book
and growing. newly_dormant is a band-style boolean like stale_contact: safe
for the model boundary, added to the rules allow-list, and exposed through
llm_client_context so a campaign can target the current leak as a cohort.

CREATE OR REPLACE can only append a column to a view, never reorder or drop
one, which is exactly the additive change this is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "11c3e0f1020d"
down_revision: str | Sequence[str] | None = "b3ef3bf2c1ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAFE = "ace_safe"

_WIDENED_CONTEXT_SQL = """
CREATE OR REPLACE VIEW llm_client_context AS
SELECT
    client_id,
    recency_band,
    value_band,
    cadence_band,
    hold_band,
    purchase_depth,
    trend_band,
    exit_reason,
    fund_type,
    in_wave,
    has_depth,
    staged_exit,
    stale_contact,
    newly_dormant
FROM client_features
"""

_NARROWED_CONTEXT_SQL = """
CREATE VIEW llm_client_context AS
SELECT
    client_id,
    recency_band,
    value_band,
    cadence_band,
    hold_band,
    purchase_depth,
    trend_band,
    exit_reason,
    fund_type,
    in_wave,
    has_depth,
    staged_exit,
    stale_contact
FROM client_features
"""


def upgrade() -> None:
    op.add_column(
        "client_features",
        sa.Column("newly_dormant", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(_WIDENED_CONTEXT_SQL)


def downgrade() -> None:
    # CREATE OR REPLACE cannot drop a column, so the view is dropped and
    # recreated at its narrower shape rather than replaced back down.
    op.execute("DROP VIEW IF EXISTS llm_client_context")
    op.execute(_NARROWED_CONTEXT_SQL)
    op.execute(f"GRANT SELECT ON llm_client_context TO {SAFE}")
    op.drop_column("client_features", "newly_dormant")
