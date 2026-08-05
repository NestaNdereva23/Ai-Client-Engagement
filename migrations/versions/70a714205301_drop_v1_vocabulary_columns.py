"""drop v1 vocabulary columns

Revision ID: 70a714205301
Revises: fa894fc0413a
Create Date: 2026-08-04 22:40:00.000000

Removes archetype, recency_bucket, value_tier, and rhythm_band from
client_features, and the long-unmaintained net_flow from clients. v1 and v2,
the only rule sets that ever matched on these, closed their windows in
fa894fc0413a; nothing resolves against them anymore.

llm_client_context selects straight from client_features, so it has to be
dropped and recreated without the four columns before they can go -- CREATE
OR REPLACE can only append a column, never drop one. The downgrade restores
the view and the columns, but not the data they held: a column drop is not
data-reversible, only schema-reversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "70a714205301"
down_revision: str | Sequence[str] | None = "fa894fc0413a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAFE = "ace_safe"

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

_WIDENED_CONTEXT_SQL = """
CREATE OR REPLACE VIEW llm_client_context AS
SELECT
    client_id,
    archetype,
    recency_bucket,
    value_tier AS value_tier_label,
    rhythm_band,
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
    op.execute("DROP VIEW IF EXISTS llm_client_context")
    op.execute(_NARROWED_CONTEXT_SQL)
    op.execute(f"GRANT SELECT ON llm_client_context TO {SAFE}")

    op.drop_column("client_features", "archetype")
    op.drop_column("client_features", "recency_bucket")
    op.drop_column("client_features", "value_tier")
    op.drop_column("client_features", "rhythm_band")
    op.drop_column("clients", "net_flow")


def downgrade() -> None:
    op.add_column("clients", sa.Column("net_flow", sa.Float(), nullable=False, server_default="0"))
    op.add_column(
        "client_features", sa.Column("archetype", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "client_features",
        sa.Column("recency_bucket", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "client_features", sa.Column("value_tier", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "client_features", sa.Column("rhythm_band", sa.Text(), nullable=False, server_default="")
    )

    op.execute("DROP VIEW IF EXISTS llm_client_context")
    op.execute(_WIDENED_CONTEXT_SQL)
    op.execute(f"GRANT SELECT ON llm_client_context TO {SAFE}")
