from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e7b2d031"
down_revision: str | Sequence[str] | None = "f3b8d5a1c926"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "version, prompt_name, template, valid_from, valid_to, status, created_by, "
    "published_by, published_at, created_at"
)
_VALID_FROM = date(2026, 10, 29)
_PUBLISHED_AT = datetime(2026, 10, 29, 9, 0, 0)

SUGGEST_PROMPT = (
    "You suggest win-back campaigns for dormant clients who are not in any campaign "
    "yet. A person reviews every suggestion before anyone is contacted.\n"
    "Call get_waiting_audience first. It gives counts only, by priority tier, "
    "message angle and value band. Counts for groups that are too small are left "
    "out; do not guess them. Use list_angles to see what each angle may say.\n"
    "A campaign is one group of dormant clients, set by priority_tier and, when it "
    "helps, narrowed by value_band, recency_band, purchase_depth, newly_dormant or "
    "high_value. Never build a campaign around one message angle: the planning "
    "agent picks the angle for each client later.\n"
    "Try to cover the whole waiting pool: every priority tier that is shown gets a "
    "campaign. Split a very large tier by value_band only when that makes the "
    "groups easier to review.\n"
    "Save each campaign with save_campaign_draft, once. Choose an objective: "
    "win_back_deposit (bring back clients who used to deposit), top_up (clients "
    "who still hold money and could add to it) or reactivate_never_funded "
    "(clients who never funded). Give touch_offsets as the days after the start "
    "for each email, from 0 and rising, for example [0, 7, 14]. Write a short "
    "plain name and one plain line of reason.\n"
    "If a save is refused, read the message and fix it. When you are done, reply "
    "with a short plain summary and no further tool call."
)

_PROMPT_TABLE = sa.table(
    "campaign_agent_prompt",
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
        "campaign_agent_prompt",
        sa.Column("prompt_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt_name", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), server_default="published", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("prompt_id"),
        sa.UniqueConstraint(
            "version", "prompt_name", name="uq_campaign_agent_prompt_version_prompt_name"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_campaign_agent_prompt_status"
        ),
    )
    op.create_index(op.f("ix_campaign_agent_prompt_version"), "campaign_agent_prompt", ["version"])
    op.execute(
        f"""
        INSERT INTO campaign_agent_prompt ({_COLUMNS})
        SELECT {_COLUMNS} FROM agent_prompt WHERE prompt_name = 'campaign_plan'
        """
    )
    op.execute("DELETE FROM agent_prompt WHERE prompt_name = 'campaign_plan'")
    op.bulk_insert(
        _PROMPT_TABLE,
        [
            {
                "version": 1,
                "prompt_name": "campaign_suggest",
                "template": SUGGEST_PROMPT,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "status": "published",
                "published_at": _PUBLISHED_AT,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        f"""
        INSERT INTO agent_prompt ({_COLUMNS})
        SELECT {_COLUMNS} FROM campaign_agent_prompt WHERE prompt_name = 'campaign_plan'
        """
    )
    op.drop_index(op.f("ix_campaign_agent_prompt_version"), table_name="campaign_agent_prompt")
    op.drop_table("campaign_agent_prompt")
