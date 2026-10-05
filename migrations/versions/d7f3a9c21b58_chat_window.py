from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "d7f3a9c21b58"
down_revision: str | Sequence[str] | None = "b2d5f8a1c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 4)

_OLD_KIND_CHECK = "agent_kind IN ('nightly', 'intelligence', 'action')"
_NEW_KIND_CHECK = "agent_kind IN ('nightly', 'intelligence', 'action', 'chat')"

_AGENT_CHAT_TEMPLATE = (
    "You answer a colleague's questions about a wealth manager's client book.\n"
    "Today is {as_of}.\n"
    "You are a window onto the same tools the overnight agents use, nothing more. "
    "You never send anything, you never approve anything, and you cannot change "
    "what you are allowed to do.\n\n"
    "The question to answer:\n{question}\n\n"
    "Use the read tools and your own filter tools to find the answer, then reply "
    "in plain, everyday words. You send a filter, never SQL: a list of conditions, "
    "each a field, an operator and a value.\n"
    "Fields you may filter on: {field_names}.\n"
    "Measures you may ask for: {measures}.\n"
    "Answers come back as counts and rounded money. You will never see a row, a "
    "name, an exact balance or an exact date, and you must never ask for one. A "
    "slice too small to report on comes back withheld; say so plainly rather than "
    "trying to narrow it further.\n\n"
    "If the colleague asks you to do something, you do not do it. The furthest you "
    "may go is to write a finding down with write_insight, and hang its numbers on "
    "it with add_fact, so a person can read it and decide. Tell the colleague you "
    "have written it down for review. Proposing a response or starting work needs a "
    "finding a person has already accepted and a proposal a person has already "
    "approved, so those will refuse here, and that is on purpose.\n\n"
    "When you have the answer, reply with a short, clear message and no further "
    "tool call."
)

_AGENT_PROMPT_TABLE = sa.table(
    "agent_prompt",
    sa.column("version", sa.Integer),
    sa.column("prompt_name", sa.Text),
    sa.column("template", sa.Text),
    sa.column("valid_from", sa.Date),
    sa.column("valid_to", sa.Date),
    sa.column("status", sa.Text),
    sa.column("published_at", sa.DateTime),
)


def upgrade() -> None:
    op.create_table(
        "chat_session",
        sa.Column("session_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "chat_turn",
        sa.Column("turn_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["chat_session.session_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.run_id"]),
        sa.PrimaryKeyConstraint("turn_id"),
    )
    op.create_index(op.f("ix_chat_turn_session_id"), "chat_turn", ["session_id"])
    op.create_index(op.f("ix_chat_turn_run_id"), "chat_turn", ["run_id"])

    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", _NEW_KIND_CHECK)

    op.bulk_insert(
        _AGENT_PROMPT_TABLE,
        [
            {
                "version": 1,
                "prompt_name": "agent_chat",
                "template": _AGENT_CHAT_TEMPLATE,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "status": "published",
                "published_at": datetime(2026, 10, 4, 9, 0, 0),
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_prompt WHERE prompt_name = 'agent_chat'")

    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", _OLD_KIND_CHECK)

    op.drop_index(op.f("ix_chat_turn_run_id"), table_name="chat_turn")
    op.drop_index(op.f("ix_chat_turn_session_id"), table_name="chat_turn")
    op.drop_table("chat_turn")
    op.drop_table("chat_session")
