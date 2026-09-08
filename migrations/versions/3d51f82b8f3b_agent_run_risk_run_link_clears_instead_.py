"""agent run risk run link clears instead of blocking cleanup

An agent run only records which risk run's data was on file when it
started; it never depends on that risk run existing. Losing the risk run
later should clear the link, not block deleting the risk run.

Revision ID: 3d51f82b8f3b
Revises: ad0397a6085a
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3d51f82b8f3b"
down_revision: str | Sequence[str] | None = "ad0397a6085a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_agent_run_risk_run_id", "agent_run", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_run_risk_run_id",
        "agent_run",
        "risk_run",
        ["risk_run_id"],
        ["run_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_run_risk_run_id", "agent_run", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_run_risk_run_id", "agent_run", "risk_run", ["risk_run_id"], ["run_id"]
    )
