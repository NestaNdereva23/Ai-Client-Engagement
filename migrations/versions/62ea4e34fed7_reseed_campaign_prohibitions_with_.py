"""reseed campaign prohibitions with contact channel rule

Revision ID: 62ea4e34fed7
Revises: b7e3a1c9d2f4
Create Date: 2026-10-22 10:43:38.015121
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "62ea4e34fed7"
down_revision: str | Sequence[str] | None = "b7e3a1c9d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_VERSION = 1

_OLD_CAMPAIGN_PROHIBITIONS = [
    "Never state how many times the client invested. Only part of their history "
    "is visible, so any count would be wrong for much of this population.",
    "Never mention a balance, an amount still invested, or money waiting in an "
    "account. Every client here holds none.",
    "Never imply when the client first invested, or how long the relationship "
    "lasted in total. Only their recent activity is visible.",
    "Never state a number that is not in the facts you were given. Do not "
    "calculate, round, or combine them into a new one.",
    "Never promise a return, a rate, or a guarantee.",
    "Never open the email by asking the client to confirm their contact details, "
    "identity, or preferred email address. Do not use contact verification as a "
    "generic opener, a personalization technique, or a safety habit, only when the "
    "selected angle's ask itself is to verify contact details.",
    "Never ask the client to explain, justify, or confirm a historical transaction "
    "or account event, unless the selected angle's ask is itself that diagnostic "
    "question. Facts about how or why an account event happened are for choosing "
    "the angle, not for putting in front of the client.",
]

_NEW_CAMPAIGN_PROHIBITIONS = [
    "Never state how many times the client invested. Only part of their history "
    "is visible, so any count would be wrong for much of this population.",
    "Never mention a balance, an amount still invested, or money waiting in an "
    "account. Every client here holds none.",
    "Never imply when the client first invested, or how long the relationship "
    "lasted in total. Only their recent activity is visible.",
    "Never state a number that is not in the facts you were given. Do not "
    "calculate, round, or combine them into a new one.",
    "Never promise a return, a rate, or a guarantee.",
    "Never write an email address, phone number, or any other contact channel "
    "anywhere in the subject or body, including in a sign-off or closing line. "
    "This applies even to the fund's own publicly known contact details: omit a "
    "closing contact line entirely rather than including one.",
    "Never open the email by asking the client to confirm their contact details, "
    "identity, or preferred email address. Do not use contact verification as a "
    "generic opener, a personalization technique, or a safety habit, only when the "
    "selected angle's ask itself is to verify contact details.",
    "Never ask the client to explain, justify, or confirm a historical transaction "
    "or account event, unless the selected angle's ask is itself that diagnostic "
    "question. Facts about how or why an account event happened are for choosing "
    "the angle, not for putting in front of the client.",
]


def upgrade() -> None:
    # The in-code CAMPAIGN_PROHIBITIONS constant picked up a rule against
    # writing contact channels into a draft, but the v1 safety_policy rows
    # seeded for email and sms were never updated to match.
    safety_policy = sa.table(
        "safety_policy",
        sa.column("channel", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("campaign_prohibitions", JSONB),
    )
    op.execute(
        safety_policy.update()
        .where(safety_policy.c.version == _POLICY_VERSION)
        .where(safety_policy.c.channel.in_(["email", "sms"]))
        .values(campaign_prohibitions=_NEW_CAMPAIGN_PROHIBITIONS)
    )


def downgrade() -> None:
    safety_policy = sa.table(
        "safety_policy",
        sa.column("channel", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("campaign_prohibitions", JSONB),
    )
    op.execute(
        safety_policy.update()
        .where(safety_policy.c.version == _POLICY_VERSION)
        .where(safety_policy.c.channel.in_(["email", "sms"]))
        .values(campaign_prohibitions=_OLD_CAMPAIGN_PROHIBITIONS)
    )
