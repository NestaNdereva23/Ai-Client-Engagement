from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e8a2c6f4b1d7"
down_revision: str | Sequence[str] | None = "d3b9e5a1c7f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Current rows move now so the first nightly run does not log every client as a route move.
    op.execute(
        """
        UPDATE client_risk_features
        SET route = 'fa_action', queue_rank = NULL
        WHERE route IS NOT NULL AND route <> 'fa_action'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE client_risk_features AS features
        SET route = latest.route, queue_rank = latest.queue_rank
        FROM (
            SELECT DISTINCT ON (client_id, unit_fund_id)
                client_id, unit_fund_id, route, queue_rank
            FROM risk_snapshot
            WHERE route IS NOT NULL AND route <> 'fa_action'
            ORDER BY client_id, unit_fund_id, snapshot_id DESC
        ) AS latest
        WHERE features.client_id = latest.client_id
            AND features.unit_fund_id = latest.unit_fund_id
            AND features.route = 'fa_action'
        """
    )
