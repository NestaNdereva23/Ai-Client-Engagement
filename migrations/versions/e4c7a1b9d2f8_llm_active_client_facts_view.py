"""llm_active_client_facts view: the month an account runs out

Revision ID: e4c7a1b9d2f8
Revises: d3b8f6c1a9e4
Create Date: 2026-09-09 15:30:00.000000

The fee warning has to name the month, so the month has to be a fact rather
than something a message works out for itself. It is coarsened to a month and
a year in SQL, so no exact date is ever SELECT-able by the safe role, and it
is a third view rather than a wider one so it can be granted and taken away on
its own.

The client's largest holding is the one that decides the month: that is the
account the warning is about.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e4c7a1b9d2f8"
down_revision: str | Sequence[str] | None = "d3b8f6c1a9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAFE = "ace_safe"

_VIEW_SQL = """
CREATE VIEW llm_active_client_facts AS
SELECT
    client_id,
    TO_CHAR(
        CURRENT_DATE + make_interval(months => GREATEST(FLOOR(months_until_empty)::int, 0)),
        'FMMonth YYYY'
    ) AS month_the_account_empties
FROM (
    SELECT DISTINCT ON (client_id) client_id, months_until_empty
    FROM active_client_fund
    WHERE months_until_empty IS NOT NULL AND balance > 0
    ORDER BY client_id, balance DESC
) largest_holding
"""


def upgrade() -> None:
    op.execute(_VIEW_SQL)
    op.execute(f"GRANT SELECT ON llm_active_client_facts TO {SAFE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON llm_active_client_facts FROM {SAFE}")
    op.execute("DROP VIEW IF EXISTS llm_active_client_facts")
