"""template_generation_plan.failed_errors

Revision ID: d8f3a6c1b9e4
Revises: c7e2a4b9f1d6
Create Date: 2026-08-21 10:00:00.000000

draft_templates_for_campaign used to let an unexpected error from one
bucket (a provider timeout, a network blip) propagate out of the whole
call, rolling back every template already drafted ahead of it in the same
run. It now catches that per bucket and keeps going; failed_errors is
where that count is recorded, separately from failed_guardrails, which
counts buckets that reached a guardrail verdict and were rejected on the
merits. Backfilled to 0: no existing plan row was affected by an error
that didn't already end the whole call.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f3a6c1b9e4"
down_revision: str | Sequence[str] | None = "c7e2a4b9f1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "template_generation_plan",
        sa.Column("failed_errors", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_tgp_failed_errors_nonneg", "template_generation_plan", "failed_errors >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_tgp_failed_errors_nonneg", "template_generation_plan", type_="check")
    op.drop_column("template_generation_plan", "failed_errors")
