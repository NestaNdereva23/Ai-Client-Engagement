"""message_angle_catalog table and the twelve angle briefs

Revision ID: e6a3c8d5f2b1
Revises: d5f2a9c7e1b4
Create Date: 2026-08-03 14:00:00.000000

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6a3c8d5f2b1"
down_revision: str | Sequence[str] | None = "d5f2a9c7e1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 8, 1)

# In the order the assignment rules are applied: facts that constrain what may
# be said sit above behavioural patterns, specific patterns above general ones,
# and the last is a deliberate general case so every client is covered.
_ANGLES = [
    (
        "not_a_goodbye",
        "Let's put this right",
        "Final sale was a bill payment or interest sweep, not a withdrawal they asked for",
        "The balance settled to zero through a charge, not a decision",
        "Confirm what happened, then offer to restart",
        "Never say they chose to leave or refer to a redemption decision",
    ),
    (
        "wrong_shelf",
        "A better fit for how you invest",
        "High Yield Fund clients who exited within six months of their last purchase",
        "They bought a long horizon product and needed their money back quickly",
        "Offer the money market fund as the liquid alternative they actually needed",
        "Never imply the original choice was a mistake on their part",
    ),
    (
        "see_what_changed",
        "See what has changed",
        "Held a year or more, exited in the mass window, with real depth of relationship",
        "A long and committed holding that came to an end",
        "Acknowledge the time they were invested, lead with what has changed since, "
        "invite a conversation",
        "Never mention that other clients left at the same time, never open with "
        "'we noticed you have been away', and never blame the client",
    ),
    (
        "the_long_hold",
        "You trusted us for years",
        "Held a year or more, exited in the mass window, but shallow purchase history",
        "One position, left untouched for years, then withdrawn",
        "Thank them for the tenure and show what the fund looks like now",
        "Never claim a savings habit or cadence they never had, and never mention "
        "that other clients left at the same time",
    ),
    (
        "your_next_deposit",
        "Ready for your next deposit",
        "Repeat investors who consistently withdrew within two months of paying in",
        "They used the fund as a holding account for money awaiting a purpose",
        "Next time money is waiting, put it here again. Very low commitment",
        "Never frame this as a long term investment pitch. That is not what they wanted",
    ),
    (
        "second_try",
        "Worth a second try",
        "A single purchase, withdrawn within two months, never returned",
        "They tested the product once, briefly, and did not come back",
        "Ask what stopped them. A question, not an offer",
        "Never assume dissatisfaction and never reference a relationship they did not have",
    ),
    (
        "you_wound_down",
        "What would change your mind",
        "Two visible sales spread six months or more apart",
        "A deliberate, gradual withdrawal rather than a single change of mind",
        "Treat it as a considered decision and ask what would reverse it",
        "Never treat a staged exit as an oversight or a lapse",
    ),
    (
        "you_were_scaling",
        "You were building something",
        "Contributions rising steadily right up to the exit",
        "Their commitment was increasing at the moment they left",
        "Something interrupted a growing position. Find out what and fix it",
        "Never suggest they lost interest. The data says the opposite",
    ),
    (
        "you_were_fading",
        "Tell us what changed",
        "Contributions shrinking steadily before the exit",
        "Engagement was declining for a while before the final sale",
        "Diagnose, do not pitch. Ask what changed",
        "Never open with a product offer. The relationship cooled first",
    ),
    (
        "back_on_schedule",
        "Restart the standing order",
        "Five or more purchases on a tight cadence of 45 days or less",
        "A genuine, measurable savings rhythm that stopped",
        "Resume the exact cadence they already had. Name the interval",
        "Never state an exact purchase count. The API censors it at five",
    ),
    (
        "onboarding_retry",
        "Let's get you started properly",
        "A single purchase and no second one, whatever the hold time",
        "They opened a relationship that never developed",
        "Ask what stopped them, and make the second step small",
        "Never reference a history, a rhythm or a pattern. There is none",
    ),
    (
        "pick_up_again",
        "Pick up where you left off",
        "Everything else: some history, no dominant signal",
        "A real but loose relationship with no single defining feature",
        "Low commitment, open ended reintroduction",
        "Never invent specificity. This angle is the honest generic one",
    ),
]


def upgrade() -> None:
    op.create_table(
        "message_angle_catalog",
        sa.Column("catalog_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("who", sa.Text(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("ask", sa.Text(), nullable=False),
        sa.Column("never", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("catalog_id"),
        sa.UniqueConstraint("version", "angle", name="uq_message_angle_catalog_version_angle"),
    )
    op.create_index(
        "ix_message_angle_catalog_version", "message_angle_catalog", ["version"], unique=False
    )

    seed = sa.table(
        "message_angle_catalog",
        sa.column("version", sa.Integer),
        sa.column("angle", sa.Text),
        sa.column("headline", sa.Text),
        sa.column("who", sa.Text),
        sa.column("claim", sa.Text),
        sa.column("ask", sa.Text),
        sa.column("never", sa.Text),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
    )
    op.bulk_insert(
        seed,
        [
            {
                "version": 1,
                "angle": angle,
                "headline": headline,
                "who": who,
                "claim": claim,
                "ask": ask,
                "never": never,
                "valid_from": _VALID_FROM,
                "valid_to": None,
            }
            for (angle, headline, who, claim, ask, never) in _ANGLES
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_message_angle_catalog_version", table_name="message_angle_catalog")
    op.drop_table("message_angle_catalog")
