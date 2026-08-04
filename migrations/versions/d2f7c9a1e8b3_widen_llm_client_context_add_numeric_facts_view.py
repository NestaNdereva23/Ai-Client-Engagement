"""widen llm_client_context, add llm_client_numeric_facts view

Revision ID: d2f7c9a1e8b3
Revises: b3d8f1a4c9e6
Create Date: 2026-08-03 21:00:00.000000

Two views rather than widening one further: bands are low sensitivity and
the existing grant already covers them, real figures are higher sensitivity
and now live behind a separate, independently revocable grant. Neither view
can return an exact calendar date; month_they_left is coarsened to YYYY-MM
in SQL, so day precision is never SELECT-able by the safe role at all.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f7c9a1e8b3"
down_revision: str | Sequence[str] | None = "b3d8f1a4c9e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAFE = "ace_safe"

_ORIGINAL_CONTEXT_SQL = """
CREATE VIEW llm_client_context AS
SELECT
    client_id,
    archetype,
    recency_bucket,
    value_tier AS value_tier_label,
    rhythm_band
FROM client_features
"""

# Adds the twelve v2 bands after the original four columns. CREATE OR REPLACE
# can only append columns, never reorder or drop one, which is exactly the
# additive change this is.
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

# Real figures, still unrounded here. Rounding to two significant figures is
# ModelFactBlock's job at construction, not this view's; rounding twice would
# let a caller who skips ModelFactBlock slip past the boundary's own check
# for an already-rounded value.
_NUMERIC_FACTS_SQL = """
CREATE VIEW llm_client_numeric_facts AS
SELECT
    client_id,
    days_cold / 365.25 AS years_since_exit,
    avg_ticket AS typical_contribution_kes,
    max_ticket AS largest_contribution_kes,
    CASE WHEN rhythm_days >= 1 THEN rhythm_days::integer END AS invested_every_n_days,
    hold_days AS days_held_after_last_topup,
    TO_CHAR(exit_date, 'YYYY-MM') AS month_they_left
FROM client_fund
WHERE is_primary_contact_row
"""


def upgrade() -> None:
    op.execute(_WIDENED_CONTEXT_SQL)
    op.execute(_NUMERIC_FACTS_SQL)
    op.execute(f"GRANT SELECT ON llm_client_numeric_facts TO {SAFE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON llm_client_numeric_facts FROM {SAFE}")
    op.execute("DROP VIEW IF EXISTS llm_client_numeric_facts")
    # CREATE OR REPLACE cannot drop columns, so the widened view is dropped
    # and recreated at its original shape rather than replaced back down.
    op.execute("DROP VIEW IF EXISTS llm_client_context")
    op.execute(_ORIGINAL_CONTEXT_SQL)
    op.execute(f"GRANT SELECT ON llm_client_context TO {SAFE}")
