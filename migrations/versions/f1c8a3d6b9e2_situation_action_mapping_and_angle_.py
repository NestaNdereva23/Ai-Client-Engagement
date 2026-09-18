"""situation_action_mapping table, and angle families

Splits the fixed situation-to-action rule table (propose.py's GROUP_ACTIONS)
into a versioned database mapping of situation, action, objective, angle,
evidence and channel, so a situation gaining a second action, or an action
picking up a second situation, is a data change instead of a code change.
Seeded from the exact seven mappings GROUP_ACTIONS already carries, so
nothing about tonight's proposals changes.

Also adds a nullable family column to message_angle_catalog and backfills it
for every angle in force today, grouping the fifteen angles into four
families: onboarding, reengagement, investment habit, and fit and guidance.

Revision ID: f1c8a3d6b9e2
Revises: 24e8030b3e05
Create Date: 2026-09-15 15:00:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

from app.agents.situation_action_mapping import MappingSpec

revision: str = "f1c8a3d6b9e2"
down_revision: str | Sequence[str] | None = "24e8030b3e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 9, 15)

_MAPPINGS = [
    MappingSpec(
        situation="signed_up_recently",
        action_code="welcome_and_top_up",
        objective="welcome",
        angle="welcome_and_top_up",
        evidence_required=(
            "A first deposit date inside the recent window, exactly one "
            "deposit on the fund, and a balance above zero"
        ),
        channel="email",
    ),
    MappingSpec(
        situation="fees_will_empty",
        action_code="fee_warning",
        objective="protect",
        angle="fee_warning",
        evidence_required="Months until empty below the threshold, and a balance above zero",
        channel="email",
    ),
    MappingSpec(
        situation="very_small_and_quiet",
        action_code="start_win_back",
        objective="reengage",
        angle="not_a_goodbye",
        evidence_required="A balance under the small balance threshold and the quiet deposits flag",
        channel="email",
    ),
    MappingSpec(
        situation="getting_smaller",
        action_code="ask_what_changed",
        objective="clarify",
        angle="see_what_changed",
        evidence_required="The shrinking deposits flag",
        channel="email",
    ),
    MappingSpec(
        situation="healthy_one_fund",
        action_code="suggest_second_fund",
        objective="encourage",
        angle="wrong_shelf",
        evidence_required="A risk band of none or low, and exactly one fund held by that client",
        channel="email",
    ),
    MappingSpec(
        situation="waiting_on_a_call",
        action_code="follow_up_when_no_one_called",
        objective="follow_up",
        angle="sitting_still",
        evidence_required=(
            "An open call task older than two days with no interaction recorded since"
        ),
        channel="email",
    ),
    MappingSpec(
        situation="more_urgent_but_not_called",
        action_code="check_in_rising_risk",
        objective="protect",
        angle="sitting_still",
        evidence_required=(
            "The client's queue position moved to a more urgent route since the run "
            "before, with nothing logged against them since"
        ),
        channel="email",
    ),
]

_ANGLE_FAMILIES = {
    "onboarding": ("welcome_and_top_up", "onboarding_retry"),
    "reengagement": (
        "not_a_goodbye",
        "second_try",
        "the_long_hold",
        "see_what_changed",
        "you_wound_down",
        "pick_up_again",
    ),
    "investment_habit": (
        "back_on_schedule",
        "your_next_deposit",
        "you_were_scaling",
        "you_were_fading",
        "sitting_still",
    ),
    "fit_and_guidance": ("wrong_shelf", "fee_warning"),
}

_ANGLE_CATALOG_TABLE = sa.table(
    "message_angle_catalog", sa.column("angle", sa.Text), sa.column("family", sa.Text)
)


def upgrade() -> None:
    op.add_column("message_angle_catalog", sa.Column("family", sa.Text(), nullable=True))
    for family, angles in _ANGLE_FAMILIES.items():
        op.execute(
            _ANGLE_CATALOG_TABLE.update()
            .where(_ANGLE_CATALOG_TABLE.c.angle.in_(angles))
            .values(family=family)
        )

    op.create_table(
        "situation_action_mapping",
        sa.Column("mapping_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("situation", sa.Text(), nullable=False),
        sa.Column("action_code", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("evidence_required", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("mapping_id"),
        sa.UniqueConstraint(
            "version",
            "situation",
            "action_code",
            name="uq_situation_action_mapping_version_situation_action",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_situation_action_mapping_status",
        ),
    )
    op.create_index(
        op.f("ix_situation_action_mapping_version"), "situation_action_mapping", ["version"]
    )

    # priority is not a column here yet (a later migration adds it), so this
    # seeds against a table snapshot matching exactly what create_table just
    # built above, rather than the live ORM model, which already carries it.
    situation_action_mapping = sa.table(
        "situation_action_mapping",
        sa.column("version", sa.Integer),
        sa.column("situation", sa.Text),
        sa.column("action_code", sa.Text),
        sa.column("objective", sa.Text),
        sa.column("angle", sa.Text),
        sa.column("evidence_required", sa.Text),
        sa.column("channel", sa.Text),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("status", sa.Text),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    published_at = datetime.now(UTC)
    op.bulk_insert(
        situation_action_mapping,
        [
            {
                "version": 1,
                "situation": spec.situation,
                "action_code": spec.action_code,
                "objective": spec.objective,
                "angle": spec.angle,
                "evidence_required": spec.evidence_required,
                "channel": spec.channel,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "status": "published",
                "published_at": published_at,
            }
            for spec in _MAPPINGS
        ],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_situation_action_mapping_version"), table_name="situation_action_mapping"
    )
    op.drop_table("situation_action_mapping")
    op.execute("DELETE FROM active_configuration WHERE component_type = 'situation_action_mapping'")

    op.drop_column("message_angle_catalog", "family")
