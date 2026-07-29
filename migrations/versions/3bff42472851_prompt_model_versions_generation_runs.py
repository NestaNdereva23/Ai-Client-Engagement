"""prompt and model version registry, generation runs

Revision ID: 3bff42472851
Revises: b8e1f4d2a9c7
Create Date: 2026-07-28 15:11:05.199644

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3bff42472851"
down_revision: str | Sequence[str] | None = "b8e1f4d2a9c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("model_version_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("model_version_id"),
        sa.UniqueConstraint("config_hash"),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("prompt_version_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("prompt_variant", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("template_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("prompt_version_id"),
        sa.UniqueConstraint("template_hash"),
    )
    op.create_table(
        "generation_runs",
        sa.Column("run_id", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("product", sa.Text(), nullable=True),
        sa.Column("prompt_version_id", sa.BigInteger(), nullable=False),
        sa.Column("model_version_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("failed_guardrail", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ai_draft_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.client_id"]),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.model_version_id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.prompt_version_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_generation_runs_client_id", "generation_runs", ["client_id"])
    op.create_index("ix_generation_runs_trace_id", "generation_runs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_generation_runs_trace_id", table_name="generation_runs")
    op.drop_index("ix_generation_runs_client_id", table_name="generation_runs")
    op.drop_table("generation_runs")
    op.drop_table("prompt_versions")
    op.drop_table("model_versions")
