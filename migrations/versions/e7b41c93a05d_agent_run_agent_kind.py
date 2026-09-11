"""agent_run: which agent the run belongs to, and the finding it answers

Revision ID: e7b41c93a05d
Revises: d2c6a08b5e41
Create Date: 2026-09-09 09:30:00.000000

Three kinds of run now share this table. Two of them read the whole book and
take a while, so only one of those may go at a time. The third answers one
finding a person has just accepted and has to start the moment they ask,
whatever else is going. Telling them apart in the row is what lets the one
at a time rule apply to the first two only, and it also lets a run list say
what it is looking at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7b41c93a05d"
down_revision: str | Sequence[str] | None = "d2c6a08b5e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column("agent_kind", sa.Text(), nullable=False, server_default="nightly"),
    )
    op.add_column("agent_run", sa.Column("insight_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_agent_run_insight_id_agent_insight",
        "agent_run",
        "agent_insight",
        ["insight_id"],
        ["insight_id"],
    )
    op.create_index(op.f("ix_agent_run_insight_id"), "agent_run", ["insight_id"])
    op.create_check_constraint(
        "ck_agent_run_agent_kind",
        "agent_run",
        "agent_kind IN ('nightly', 'intelligence', 'action')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.drop_index(op.f("ix_agent_run_insight_id"), table_name="agent_run")
    op.drop_constraint("fk_agent_run_insight_id_agent_insight", "agent_run", type_="foreignkey")
    op.drop_column("agent_run", "insight_id")
    op.drop_column("agent_run", "agent_kind")
