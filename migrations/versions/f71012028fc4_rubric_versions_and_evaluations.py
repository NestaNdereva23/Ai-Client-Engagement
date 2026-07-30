"""rubric versions and evaluations

Revision ID: f71012028fc4
Revises: 0b02b742d728
Create Date: 2026-07-30 08:54:48.557473

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f71012028fc4"
down_revision: str | Sequence[str] | None = "0b02b742d728"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rubric_versions",
        sa.Column("rubric_version_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rubric_text", sa.Text(), nullable=False),
        sa.Column("rubric_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("rubric_version_id"),
        sa.UniqueConstraint("rubric_hash"),
    )

    op.create_table(
        "evaluations",
        sa.Column("evaluation_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("rubric_version_id", sa.BigInteger(), nullable=False),
        sa.Column("model_version_id", sa.BigInteger(), nullable=False),
        sa.Column("tone", sa.Integer(), nullable=False),
        sa.Column("compliance", sa.Integer(), nullable=False),
        sa.Column("grounding", sa.Integer(), nullable=False),
        sa.Column("personalization", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.model_version_id"]),
        sa.ForeignKeyConstraint(["rubric_version_id"], ["rubric_versions.rubric_version_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["generation_runs.run_id"]),
        sa.PrimaryKeyConstraint("evaluation_id"),
    )
    op.create_index("ix_evaluations_run_id", "evaluations", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_evaluations_run_id", table_name="evaluations")
    op.drop_table("evaluations")
    op.drop_table("rubric_versions")
