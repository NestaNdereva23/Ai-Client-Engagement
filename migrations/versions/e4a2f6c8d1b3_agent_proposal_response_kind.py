"""agent_proposal response_kind, backfilled from the catalog row each proposal was written against

Revision ID: e4a2f6c8d1b3
Revises: c26d540455aa
Create Date: 2026-09-16 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a2f6c8d1b3"
down_revision: str | Sequence[str] | None = "c26d540455aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_proposal", sa.Column("response_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_agent_proposal_response_kind",
        "agent_proposal",
        "response_kind IS NULL OR response_kind IN ('automated_email', 'advisor_task', "
        "'phone_call', 'campaign_enrolment', 'product_teaching', 'monitor_only', "
        "'escalate', 'change_client_state', 'ask_a_person_first')",
    )
    op.execute(
        "UPDATE agent_proposal AS p "
        "SET response_kind = c.response_kind "
        "FROM agent_action_catalog AS c "
        "WHERE c.action_code = p.action_code AND c.version = p.catalog_version "
        "AND p.response_kind IS NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_proposal_response_kind", "agent_proposal", type_="check")
    op.drop_column("agent_proposal", "response_kind")
