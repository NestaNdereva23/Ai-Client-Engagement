"""message_angle_catalog v2: use, and natural rather than reported emails

Revision ID: c7e2a4b9f1d6
Revises: b1c4e9a7d3f5
Create Date: 2026-08-21 09:15:00.000000

Version 1 sat behind a system prompt that treated every fact it was given
as fair to tell the client, which read as a database reporting on itself:
emails opened by asking the client to confirm their contact details, or
explained why an account settled to zero before pivoting to an offer. This
version rewrites every claim, ask and never to separate why a client was
selected for an angle from what the email actually says, and adds use so
each angle states how retrieved product or market facts should be treated,
from leading with a current proposition to not using one at all. wrong_shelf,
your_next_deposit, second_try, you_wound_down, you_were_scaling, and
onboarding_retry pick up an explicit ban on contact verification language
that version 1 did not carry. see_what_changed stays held, carried over from
the version 1 hold: its wording changed, the business decision behind the
hold did not.
"""

from collections.abc import Sequence
from datetime import date

from alembic import op
from sqlalchemy.orm import Session

from app.rules.catalog import AngleSpec, save_catalog_version

# revision identifiers, used by Alembic.
revision: str = "c7e2a4b9f1d6"
down_revision: str | Sequence[str] | None = "b1c4e9a7d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 8, 21)

_SEE_WHAT_CHANGED_HELD = True

# Same order and identifiers as version 1, so an existing rule set's
# prompt_variant still resolves to a row without a rules migration.
_ANGLES = [
    AngleSpec(
        angle="not_a_goodbye",
        headline="Let's put this right",
        who=("Final sale was a bill payment or interest sweep, not a withdrawal they asked for"),
        claim=(
            "Reopen the relationship and give the client a relevant reason to "
            "consider investing again, without suggesting they chose to leave"
        ),
        ask=(
            "Invite the client to explore current investment options or restart "
            "their investment relationship"
        ),
        never=(
            "Never say or imply the client chose to leave. Never ask the client to "
            "explain or confirm the historical transaction. Never mention the "
            "account settling to zero, a charge, or a balance reaching zero. Never "
            "ask the client to confirm contact details"
        ),
        use=(
            "Prefer a relevant current investment proposition from RAG as the "
            "reason to reconnect. How the account settled is internal targeting "
            "context only and must not appear in the email"
        ),
    ),
    AngleSpec(
        angle="wrong_shelf",
        headline="A better fit for how you invest",
        who="High Yield Fund clients who exited within six months of their last purchase",
        claim=(
            "The Money Market Fund may be a more liquid, better fitting option "
            "for money that needs to stay accessible"
        ),
        ask="Invite the client to consider the more liquid Money Market Fund option",
        never=(
            "Never imply the original choice was a mistake or a poor decision. "
            "Never criticize the original product. Never include contact "
            "verification language"
        ),
        use=(
            "Use current Money Market Fund information or proposition data when "
            "available. Lead with suitability and flexibility rather than "
            "criticizing the previous product"
        ),
    ),
    AngleSpec(
        angle="see_what_changed",
        headline="See what has changed",
        who="Held a year or more, exited in the mass window, with real depth of relationship",
        claim=(
            "Acknowledge the previous long and meaningful holding, and lead with "
            "what is different or relevant about the proposition today"
        ),
        ask="Invite a conversation about the current proposition",
        never=(
            "Never mention that other clients left at the same time. Never open "
            "with 'we noticed you have been away'. Never blame the client or "
            "imply dissatisfaction. Never include contact verification language"
        ),
        use=(
            "Prioritize current product or market changes as the reason to "
            "reconnect. The message should answer why the client should look "
            "again now, not restate the segmentation"
        ),
        held=_SEE_WHAT_CHANGED_HELD,
    ),
    AngleSpec(
        angle="the_long_hold",
        headline="You trusted us for years",
        who="Held a year or more, exited in the mass window, but shallow purchase history",
        claim=(
            "Acknowledge the previous relationship appropriately and show what "
            "the investment proposition looks like today"
        ),
        ask="Invite the client to explore the current proposition",
        never=(
            "Never claim a savings habit or cadence that is unsupported. Never "
            "imply repeated investing. Never mention that other clients left at "
            "the same time. Never include contact verification language"
        ),
        use=(
            "Use current product information or changes as the reason to "
            "reconnect. Historical tenure may be acknowledged only if explicitly "
            "supported by the facts given"
        ),
    ),
    AngleSpec(
        angle="your_next_deposit",
        headline="Ready for your next deposit",
        who="Repeat investors who generally withdrew relatively soon after contributing",
        claim=(
            "Position the product as a practical place for money that may need "
            "to remain accessible, not a long term investment strategy"
        ),
        ask=(
            "Invite the client to consider using the product again when they "
            "next have funds to park"
        ),
        never=(
            "Never frame this as a long term wealth building pitch. Never claim "
            "a savings habit unless explicitly supported. Never state unsupported "
            "transaction counts. Never include contact verification language"
        ),
        use=(
            "Use liquidity, accessibility, or convenience information only when "
            "explicitly supported by the supplied facts"
        ),
    ),
    AngleSpec(
        angle="second_try",
        headline="Worth a second try",
        who="A single purchase, withdrawn within two months, never returned",
        claim="Understand what prevented the relationship from continuing",
        ask="Ask what stopped them",
        never=(
            "Never assume dissatisfaction. Never assume the client disliked the "
            "product. Never reference a relationship they did not have. Never "
            "include generic contact verification statements"
        ),
        use=(
            "Do not lead with a product proposition. The purpose is to open a "
            "conversation. Use RAG only if it directly supports that conversation"
        ),
    ),
    AngleSpec(
        angle="you_wound_down",
        headline="What would change your mind",
        who="Two visible sales spread six months or more apart",
        claim=(
            "Treat the change as a considered decision and invite the client to "
            "explain what would make them reconsider"
        ),
        ask="Ask what would need to change for them to consider returning",
        never=(
            "Never characterize the behavior as accidental. Never describe the "
            "client as having forgotten about the investment. Never imply the "
            "client simply lapsed. Never include contact verification language"
        ),
        use=(
            "Current propositions may be used only if they credibly answer what "
            "might have changed. Do not force a product offer into the email"
        ),
    ),
    AngleSpec(
        angle="you_were_scaling",
        headline="You were building something",
        who="Contributions rising steadily right up to the exit",
        claim=(
            "Acknowledge that the client's investment activity had been "
            "developing and explore whether there is an opportunity to restart"
        ),
        ask=(
            "Invite a conversation about what interrupted that direction and "
            "whether restarting makes sense"
        ),
        never=(
            "Never suggest the client lost interest. Never invent a reason for "
            "the change. Never claim a specific contribution pattern unless "
            "explicitly permitted. Never include contact verification language"
        ),
        use=(
            "A current proposition can support the restart conversation, but the "
            "email should not pretend to know why the client's activity stopped"
        ),
    ),
    AngleSpec(
        angle="you_were_fading",
        headline="Tell us what changed",
        who="Contributions shrinking steadily before the exit",
        claim="Understand what changed before attempting to sell anything",
        ask="Ask what changed",
        never=(
            "Never open with a product offer. Never assume dissatisfaction. "
            "Never assume financial circumstances. Never invent a reason for "
            "declining activity"
        ),
        use=(
            "Do not lead with RAG or a product proposition. Use current "
            "information only if it naturally helps the conversation after the "
            "diagnostic purpose is established"
        ),
    ),
    AngleSpec(
        angle="back_on_schedule",
        headline="Restart the standing order",
        who="Five or more purchases on a tight cadence of 45 days or less",
        claim="Encourage the client to resume the investment rhythm they previously demonstrated",
        ask="Invite the client to restart that investment rhythm",
        never=(
            "Never state an exact purchase count. The API censors it at five. "
            "Never invent an exact purchase count. Never claim a cadence that is "
            "not explicitly provided"
        ),
        use=(
            "If the cadence interval is an approved client facing fact, it may "
            "be referenced using the permitted placeholder. Relevant product "
            "information may support the restart proposition"
        ),
    ),
    AngleSpec(
        angle="onboarding_retry",
        headline="Let's get you started properly",
        who="A single purchase and no second one, whatever the hold time",
        claim="Reopen the relationship and make the next step feel simple",
        ask="Ask what stopped the client and invite a small next step",
        never=(
            "Never reference a history, a rhythm or a pattern beyond the single "
            "supported event. Never imply dissatisfaction. Never invent a reason "
            "they did not return. Never include contact verification language"
        ),
        use=(
            "Use a relevant current proposition only if it makes the second step "
            "clearer or easier. Do not force a product pitch"
        ),
    ),
    AngleSpec(
        angle="pick_up_again",
        headline="Pick up where you left off",
        who="Everything else: some history, no dominant behavioral signal",
        claim="Provide a low commitment, open ended reintroduction",
        ask="Invite the client to reconnect and explore what may be relevant now",
        never=(
            "Never invent specificity. Never manufacture a behavioral pattern. "
            "Never claim a reason for the client's inactivity. Never force an "
            "unsupported narrative"
        ),
        use=(
            "Use relevant current product or market information when available, "
            "but only if it creates a natural reason to reconnect"
        ),
    ),
]


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_catalog_version(session, 2, _ANGLES, valid_from=_VALID_FROM)
    session.flush()


def downgrade() -> None:
    op.execute("DELETE FROM message_angle_catalog WHERE version = 2")
