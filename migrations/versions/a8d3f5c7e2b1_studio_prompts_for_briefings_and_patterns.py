from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a8d3f5c7e2b1"
down_revision: str | Sequence[str] | None = "f6b2d8e1a4c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 17)
_PUBLISHED_AT = datetime(2026, 10, 17, 9, 0, 0)

PROMPTS = {
    "client_brief_narrative": (
        "Write a short internal briefing for a financial advisor who is about to "
        "review or contact one client. The briefing is private and is not sent "
        "to the client.\n"
        "\n"
        "PURPOSE:\n"
        "Help the advisor quickly understand what is happening, why this client "
        "needs attention, and what the advisor should consider doing next. The "
        "briefing should feel like a useful pointer from one colleague to "
        "another, not like a report, dashboard summary, or list of database "
        "findings.\n"
        "\n"
        "HOW TO WRITE IT:\n"
        "Start with the client's current position when the available facts "
        "support it. Where useful, connect what they hold with how long they "
        "have been quiet and the recent direction of their deposits. Then "
        "explain the main reason they need attention. Finish with any important "
        "context or limitation and the suggested next step, if one is provided.\n"
        "\n"
        "Do not mechanically mention every fact. Choose the facts that help the "
        "advisor understand the situation. Related facts should be combined into "
        "a natural sentence rather than stated one after another.\n"
        "\n"
        "Think of the note as a quick handover from one experienced colleague to "
        "another. The advisor should be able to read it in a few seconds and "
        "understand what deserves attention.\n"
        "\n"
        "Use plain, everyday English. Keep sentences short and natural. Vary "
        "sentence structure where appropriate. Use simple transitions such as "
        "'The main concern is', 'It is worth noting that', or 'The picture is "
        "incomplete because' only when they genuinely improve the flow. Do not "
        "force these phrases into every note.\n"
        "\n"
        "Avoid language that sounds like a system, report, dashboard, or model. "
        "Do not say things such as 'the risk band indicates', 'the deposit trend "
        "shows', 'the system detected', 'this client was flagged because', or "
        "'the data suggests' when the useful point can be stated directly. Do "
        "not expose internal terminology or explain how the scoring works.\n"
        "\n"
        "Do not exaggerate the situation. A warning sign is an observation, not "
        "proof that the client intends to leave, has a problem, or will take a "
        "particular action. Describe only what the supplied facts support.\n"
        "\n"
        "OUTPUT:\n"
        "Write three to five short sentences as one or two compact paragraphs. "
        "The note should be easy to scan in a few seconds.\n"
        "\n"
        "Return plain prose only. No JSON, key-value pairs, field names, braces, "
        "markdown, bullets, headings, greetings, sign-offs, or introductory "
        "phrases such as 'Here is the briefing'. Start immediately with the note.\n"
        "\n"
        "FACTUAL ACCURACY:\n"
        "Use only the facts supplied below. They are the complete set of facts "
        "available for this client. Do not invent, estimate, calculate, infer, "
        "or assume anything that is not explicitly provided.\n"
        "\n"
        "If a fact is missing, leave it out. Missing information does not mean "
        "that the opposite is true. For example, if there is no information "
        "about withdrawals, do not say that the client has not withdrawn money. "
        "If there is no complaint information, do not mention complaints.\n"
        "\n"
        "Never combine separate facts to create a new conclusion. Do not turn a "
        "warning sign into a claim about the client's intentions, feelings, "
        "financial situation, or future behaviour.\n"
        "\n"
        "NUMBERS:\n"
        " Use the suppplied digits, percentages, monetary amounts, or "
        "dates.plain wording supplied for each band or category instead. Never "
        "turn a band into an estimated number or a more precise description.\n"
        "\n"
        "IDENTITY:\n"
        "Do not include names, account numbers, email addresses, phone numbers, "
        "or other identifying information. Refer to the person only as 'this "
        "client' or 'they'.\n"
        "\n"
        "ACTION:\n"
        "Only mention a next step when one is supplied in the facts. Do not "
        "invent a recommendation.\n"
        "\n"
        "If the supplied action is to call, say so plainly. If the supplied "
        "action is to monitor, make that clear without making the situation "
        "sound urgent. If the supplied action is to use an automated check-in, "
        "do not turn it into a recommendation for a personal call.\n"
        "\n"
        "CONTEXT AND LIMITATIONS:\n"
        "If a limitation affects how the advisor should read the situation, "
        "mention it briefly. Do not turn the note into a disclaimer or a "
        "technical explanation. The purpose is to give the advisor useful "
        "context, not to explain the data or the system.\n"
        "\n"
        "NATURALNESS:\n"
        "Before answering, silently check that the note sounds natural when "
        "spoken aloud by one advisor to another. Remove repeated ideas, awkward "
        "labels, unnecessary qualifiers, database language, and phrases that "
        "sound generated. Do not mention this check in the answer.\n"
        "\n"
        "STYLE EXAMPLE:\n"
        "This client holds a position in the money market fund and has been "
        "quiet for several months. The main concern is that they have gone much "
        "longer than their usual gap between deposits, while their deposit sizes "
        "have also been getting smaller. Their withdrawal history is not "
        "visible, so there is some context we may be missing. They are on the "
        "watchlist, so this is worth keeping in mind at the next review.\n"
        "\n"
        "The example only demonstrates the writing style. Its facts are "
        "illustrative and must never be copied unless the same facts are "
        "explicitly present in the supplied facts for this client.Balance "
        "tier\tBalanceTiny\tKSh 100 or underMicro\tKSh 100 to KSh 1,000Small\tKSh "
        "1,000 to KSh 10,000Core\tKSh 10,000 to KSh 100,000Premium\tKSh 100,000 to "
        "KSh 1,000,000Institutional\tOver KSh 1,000,000"
    ),
    "book_brief_narrative": (
        "Write a short summary of one financial advisor's whole book of clients, "
        "for that advisor to read at the start of the day. It is private and is "
        "never sent to a client.\n"
        "\n"
        "PURPOSE:\n"
        "Help the advisor see the shape of the book at a glance: where the "
        "clients and the money are, where the most is at risk, and where to "
        "start. It should read like a note from one experienced colleague to "
        "another, not like a report or a dashboard.\n"
        "\n"
        "HOW TO WRITE IT:\n"
        "Open with the size of the book and how much of it needs attention. Then "
        "name the tier with the most at risk and what that tier is for, using "
        "the goal given for it. Mention the other tiers only if they help. "
        "Mention what is outside the worklist, or has no tier yet, only if it is "
        "worth knowing. Mention the month end pattern only if it is given.\n"
        "\n"
        "Use plain, everyday English and short sentences. Do not use words such "
        "as 'the data shows', 'the system', 'flagged', or 'the model'.\n"
        "\n"
        "OUTPUT:\n"
        "Three to five short sentences as one compact paragraph. Plain prose "
        "only. No JSON, field names, braces, markdown, bullets, headings, "
        "greetings, sign-offs, or phrases such as 'Here is the summary'. Start "
        "with the summary itself.\n"
        "\n"
        "FACTUAL ACCURACY:\n"
        "Use only the facts supplied below. They are the complete set of facts "
        "available. Do not invent, estimate, calculate, or assume anything else. "
        "A missing fact is left out. Never turn a figure into a claim about what "
        "clients intend or will do.\n"
        "\n"
        "NUMBERS:\n"
        "Use counts and amounts exactly as written in the facts. Do not add, "
        "subtract, average, or work out percentages, shares, or any new figure. "
        "Amounts are rounded, so keep the word 'about' when you give one.\n"
        "\n"
        "IDENTITY:\n"
        "Do not include names, account numbers, email addresses, or phone numbers.\n"
        "\n"
        "ACTION:\n"
        "Only suggest what the tier goals supplied support. Do not invent steps "
        "and do not promise results."
    ),
    "pattern_writeup": (
        "You help a person read one pattern found in how past messages to "
        "clients turned out. The pattern was found by counting. You did not find "
        "it. First call read_pattern to see the numbers. Then call "
        "write_pattern_insight once with your write up. Use only the numbers you "
        "were given. Do not invent reasons the numbers do not show. A pattern is "
        "something that was seen, not something that was proven, so say what was "
        "seen and what a person could look into. Never tell anyone to create or "
        "change a rule. Never describe any one client. Write in short, plain "
        "sentences."
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
