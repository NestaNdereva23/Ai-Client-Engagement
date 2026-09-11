"""agent_action_catalog: which sort of response each action is

Revision ID: d2c6a08b5e41
Revises: b4d81e05c9a3
Create Date: 2026-09-09 09:00:00.000000

The agent can now answer a finding in more ways than sending an email: a
task for an adviser, a call, a place in a campaign, a note to watch and
nothing more. The code that carries an action out has to tell those apart,
and matching on a growing list of action codes in Python would drift from
what the catalogue actually holds. The sort is stored beside the action
instead. Every action that already existed sends an email, apart from the
learning note and the one that does nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2c6a08b5e41"
down_revision: str | Sequence[str] | None = "b4d81e05c9a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_BY_CODE = {
    "send_learning_note": "product_teaching",
    "do_nothing": "monitor_only",
}

_DEFAULT_KIND = "automated_email"


def upgrade() -> None:
    op.add_column(
        "agent_action_catalog",
        sa.Column("response_kind", sa.Text(), nullable=False, server_default=_DEFAULT_KIND),
    )
    for action_code, kind in _KINDS_BY_CODE.items():
        op.execute(
            sa.text(
                "UPDATE agent_action_catalog SET response_kind = :kind WHERE action_code = :code"
            ).bindparams(kind=kind, code=action_code)
        )
    op.alter_column("agent_action_catalog", "response_kind", server_default=None)
    op.create_check_constraint(
        "ck_agent_action_catalog_response_kind",
        "agent_action_catalog",
        "response_kind IN ('automated_email', 'advisor_task', 'phone_call', "
        "'campaign_enrolment', 'product_teaching', 'monitor_only', 'escalate', "
        "'change_client_state', 'ask_a_person_first')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_action_catalog_response_kind", "agent_action_catalog", type_="check"
    )
    op.drop_column("agent_action_catalog", "response_kind")
