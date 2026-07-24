"""append-only audit_log table

Revision ID: d7b3e9f4a1c2
Revises: c9f3a1b7d2e4
Create Date: 2026-07-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d7b3e9f4a1c2"
down_revision: str | Sequence[str] | None = "c9f3a1b7d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Roles allowed to append and read the trail. No role is granted UPDATE or
# DELETE, so the table stays append-only.
_WRITERS = ("ace_restricted", "ace_safe")


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("log_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("ix_audit_log_run_id", "audit_log", ["run_id"])
    op.create_index("ix_audit_log_trace_id", "audit_log", ["trace_id"])

    op.execute("REVOKE ALL ON audit_log FROM PUBLIC")
    for role in _WRITERS:
        op.execute(f"GRANT INSERT, SELECT ON audit_log TO {role}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE audit_log_log_id_seq TO {role}")


def downgrade() -> None:
    op.drop_index("ix_audit_log_trace_id", table_name="audit_log")
    op.drop_index("ix_audit_log_run_id", table_name="audit_log")
    op.drop_table("audit_log")
