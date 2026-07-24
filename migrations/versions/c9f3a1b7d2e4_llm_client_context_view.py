"""llm_client_context allow-listed view

Revision ID: c9f3a1b7d2e4
Revises: b5e7c1d9f2a3
Create Date: 2026-07-24 10:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f3a1b7d2e4"
down_revision: str | Sequence[str] | None = "b5e7c1d9f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAFE = "ace_safe"

# Explicit allow-list: only tiers and buckets, keyed by client_id for
# server-side re-attachment. A new client_features column is never exposed here
# unless it is added on purpose.
_VIEW_SQL = """
CREATE VIEW llm_client_context AS
SELECT
    client_id,
    archetype,
    recency_bucket,
    value_tier AS value_tier_label,
    rhythm_band
FROM client_features
"""


def upgrade() -> None:
    op.execute(_VIEW_SQL)
    op.execute(f"GRANT SELECT ON llm_client_context TO {SAFE}")
    # The safe path now reads only the allow-listed view, never the feature table.
    op.execute(f"REVOKE ALL ON client_features FROM {SAFE}")


def downgrade() -> None:
    op.execute(f"GRANT SELECT ON client_features TO {SAFE}")
    op.execute("DROP VIEW IF EXISTS llm_client_context")
