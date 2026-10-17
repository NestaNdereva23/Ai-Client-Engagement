"""chat_session, chat_turn and assistant_starter: client bound chat with blocks and starters"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a1c9d4b7f2"
down_revision: str | Sequence[str] | None = "c4e8a1d7b903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STARTERS = (
    (
        "quick_email",
        "quick_email",
        "Draft a quick email",
        "Warm, short and ready to review.",
        "mail",
        10,
    ),
    (
        "email_options",
        "email_options",
        "Compare email options",
        "See the kinds of email that fit, with a top pick.",
        "mail",
        20,
    ),
    (
        "deeper_brief",
        "deeper_brief",
        "Gather a deeper brief",
        "Where things stand and what to say.",
        "document",
        30,
    ),
)


def upgrade() -> None:
    op.add_column("chat_session", sa.Column("client_id", sa.BigInteger(), nullable=True))
    op.add_column("chat_session", sa.Column("unit_fund_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_chat_session_client_fund", "chat_session", ["client_id", "unit_fund_id"])
    op.add_column("chat_turn", sa.Column("intent", sa.Text(), nullable=True))
    op.add_column("chat_turn", sa.Column("action_ref", sa.Text(), nullable=True))
    op.add_column(
        "chat_turn",
        sa.Column(
            "blocks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    starter = op.create_table(
        "assistant_starter",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="100", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.bulk_insert(
        starter,
        [
            {
                "code": code,
                "intent": intent,
                "label": label,
                "description": description,
                "icon": icon,
                "sort_order": sort_order,
            }
            for code, intent, label, description, icon, sort_order in STARTERS
        ],
    )


def downgrade() -> None:
    op.drop_table("assistant_starter")
    op.drop_column("chat_turn", "blocks")
    op.drop_column("chat_turn", "action_ref")
    op.drop_column("chat_turn", "intent")
    op.drop_index("ix_chat_session_client_fund", table_name="chat_session")
    op.drop_column("chat_session", "unit_fund_id")
    op.drop_column("chat_session", "client_id")
