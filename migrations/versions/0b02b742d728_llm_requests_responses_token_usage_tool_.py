"""llm requests responses token usage tool calls trace refs

Revision ID: 0b02b742d728
Revises: 3bff42472851
Create Date: 2026-07-29 14:53:47.370639

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0b02b742d728"
down_revision: str | Sequence[str] | None = "3bff42472851"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_requests",
        sa.Column("request_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.BigInteger(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.model_version_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["generation_runs.run_id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_llm_requests_run_id", "llm_requests", ["run_id"])

    op.create_table(
        "llm_responses",
        sa.Column("response_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["llm_requests.request_id"]),
        sa.PrimaryKeyConstraint("response_id"),
        sa.UniqueConstraint("request_id"),
    )

    op.create_table(
        "token_usage",
        sa.Column("token_usage_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["llm_requests.request_id"]),
        sa.PrimaryKeyConstraint("token_usage_id"),
        sa.UniqueConstraint("request_id"),
    )

    op.create_table(
        "tool_calls",
        sa.Column("tool_call_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tool_output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["generation_runs.run_id"]),
        sa.PrimaryKeyConstraint("tool_call_id"),
    )
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id"])

    op.create_table(
        "trace_refs",
        sa.Column("run_id", sa.Text(), autoincrement=False, nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("trace_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["generation_runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_trace_refs_trace_id", "trace_refs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_trace_refs_trace_id", table_name="trace_refs")
    op.drop_table("trace_refs")
    op.drop_index("ix_tool_calls_run_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_table("token_usage")
    op.drop_table("llm_responses")
    op.drop_index("ix_llm_requests_run_id", table_name="llm_requests")
    op.drop_table("llm_requests")
