from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "f6b2d8e1a4c9"
down_revision: str | Sequence[str] | None = "e5a1c9d4b7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 16)
_PUBLISHED_AT = datetime(2026, 10, 16, 9, 0, 0)

PROMPTS = {
    "client_email_draft": (
        "Write one short email from a client's financial advisor to that client. It is "
        "a warm, plain check in. A person will read and edit it before anything is "
        "sent.\n\nRules:\n- Open with 'Dear {{first_name}},' exactly as written, braces "
        "included. It is swapped for the real first name after you write. Never invent "
        "a name.\n- Use only the facts below. Do not add any other fact about the "
        "client.\n- Write no digits at all. No amounts, dates, percentages or counts.\n- "
        "Never mention a balance, a risk, a warning sign, a score or that the client "
        "was flagged. The client must not feel watched.\n- Never tell the client what "
        "you know about their account. Do not say you noticed a deposit, a withdrawal, "
        "a gap or any change in activity, and do not say how long since their last "
        "deposit. The facts only help you pick a fitting tone.\n- Never promise a "
        "return, a rate or a guarantee.\n- Ask one easy question, such as whether they "
        "would like a short call.\n- Plain, everyday English. Under 110 words in the "
        "body. No sign off name; the advisor adds their own.\n\nReturn only a JSON "
        'object with two string fields: {"subject": "...", "body": "..."}. No '
        "markdown, no code fences, no text outside the object."
    ),
    "email_options_suggest": (
        "You help a financial advisor decide which email to send to one client. "
        "Suggest different kinds of email that could fit this client. A person reads "
        "every suggestion, picks one, and reviews the finished email before anything "
        "is sent.\n\nYou are given facts about the client as bands, not exact figures. "
        "Use only those facts.\n\nRules:\n- Give between 2 and 4 options. They must "
        "differ in approach, not only in wording.\n- Mark exactly one option as "
        "recommended: the one that fits this client best.\n- Lean toward warmth and "
        "encouragement. Never alarm the client or make them feel watched.\n- Write no "
        "digits at all anywhere in your reply.\n- You know nothing about market events "
        "or how any investment has performed. Never claim or hint at any of that.\n- "
        "Never hint that the client's recent activity was noticed, for example with "
        "phrases like 'now that things have settled' or 'after your recent move'.\n- "
        "summary and why speak to the advisor, for example 'The email thanks the "
        "client...'. Never address the client as 'you' in them.\n- title: a short plain "
        "name for the kind of email.\n- summary: one or two sentences telling the "
        "advisor what the email would say and do.\n- why: one sentence telling the "
        "advisor why this fits this client. You may name the client's situation in "
        "plain words here. This is shown to the advisor only.\n- angle: one to three "
        "sentences of instruction for the person who writes the email. Say the tone "
        "and what to talk about. The angle must never mention a balance, a withdrawal, "
        "a risk, a warning, a fee, a score or that anything about the client was "
        "noticed.\n- tags: up to three short words about the tone.\n\nReturn only a JSON "
        'object like {"options": [{"title": "...", "summary": "...", "why": "...", '
        '"angle": "...", "tags": ["..."], "recommended": true}]}. No markdown, no code '
        "fences, no text outside the object."
    ),
    "email_purpose_ask_what_changed": (
        "Ask warmly whether anything has changed for them lately. Do not say that "
        "their deposits have changed."
    ),
    "email_purpose_encourage_top_up": (
        "This client saves regularly and all is well. Thank them warmly for saving "
        "with us and encourage them to keep adding to their investment. Keep the tone "
        "upbeat and calm. Never mention a balance, a line, a limit, a fee or any risk. "
        "Instead of offering a call, end with a gentle invitation to top up whenever "
        "they are ready, and say you are glad to help if they have questions."
    ),
    "email_purpose_grow_by_staying_invested": (
        "This client uses their investment actively. Thank them warmly for investing "
        "with us. Say you would like to help their money keep growing, and that the "
        "interest it earns builds up the longer it stays invested. Do not mention any "
        "deposit, withdrawal or money moving in or out. End by offering a short call "
        "to talk about their plans."
    ),
    "email_purpose_reschedule_meeting": (
        "We missed a meeting that was planned. Say sorry we missed it and offer to find a new time."
    ),
    "email_purpose_review_goals": (
        "This client's saving has slowed. Thank them warmly for investing with us and "
        "offer a short goals review, to help them plan what to save and what to keep "
        "for spending. Do not mention any deposit, withdrawal or money moving in or "
        "out. End by offering a short call."
    ),
    "email_purpose_welcome_and_top_up": (
        "They made their first deposit and nothing since. Welcome them warmly and ask "
        "whether they would like help with a second deposit."
    ),
}

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
    op.bulk_insert(
        _AGENT_PROMPT_TABLE,
        [
            {
                "version": 1,
                "prompt_name": name,
                "template": template,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "status": "published",
                "published_at": _PUBLISHED_AT,
            }
            for name, template in PROMPTS.items()
        ],
    )


def downgrade() -> None:
    names = ", ".join(f"'{name}'" for name in PROMPTS)
    op.execute(f"DELETE FROM agent_prompt WHERE prompt_name IN ({names})")
