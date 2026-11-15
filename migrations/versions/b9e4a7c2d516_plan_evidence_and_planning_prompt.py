from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "b9e4a7c2d516"
down_revision: str | Sequence[str] | None = "f3b7d2a9c614"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 28)
_PUBLISHED_AT = datetime(2026, 10, 28, 9, 0, 0)

PROMPT_NAME = "campaign_plan"
PROMPT = (
    "You plan the next email touch for each client in one campaign.\n"
    "Each slot is one client's due touch, identified only by slot number. The slot "
    "list says whether it is a first touch or a follow up, the angle sent last time "
    "and how long ago. Call get_touch_history to read what a slot's client was "
    "already sent and what happened since, get_side_change to see whether they moved "
    "or their balance changed, and get_campaign_context for the objective and the "
    "angles you may use.\n"
    "Pick an angle from list_angles for every slot. Never use an angle, or an angle "
    "from the same family, that this client was already sent in this campaign. Do "
    "not default to one angle for every slot: spread first touch slots across "
    "different live angles. Only repeat an angle across different slots when the "
    "catalogue leaves no other fit.\n"
    "Pick how many days until each client's next touch, inside the limits given "
    "below. It does not have to be the same for every slot or every run.\n"
    "Give every slot a next_time note: one short line for the next planning run "
    "about what to follow up on, or avoid, with that client.\n"
    "If you have a subject idea, give it as suggested_subject. It must not be close "
    "to a subject this client was already sent.\n"
    "You may call search_web for a fact from an approved source, and get_verified_rate "
    "for the product's own current rate. If a fact is worth citing for a slot, copy "
    "it word for word into that slot's research_notes. Leave research_notes empty "
    "when you found nothing to cite.\n"
    "Call choose_touch_plan with a decision for every slot you were given. If it "
    "refuses, it lists every problem at once: fix them all and call it again. You "
    "get two more tries after the first. When it saves, reply with a short plain "
    "summary and make no further tool call."
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

_TO_OBJECTS = """
UPDATE touch_plan
SET cited_facts = COALESCE((
    SELECT jsonb_agg(
        CASE WHEN jsonb_typeof(item) = 'string'
             THEN jsonb_build_object('text', item #>> '{}')
             ELSE item END
        ORDER BY position
    )
    FROM jsonb_array_elements(cited_facts) WITH ORDINALITY AS facts(item, position)
), '[]'::jsonb)
WHERE cited_facts IS NOT NULL AND jsonb_typeof(cited_facts) = 'array'
"""

_TO_STRINGS = """
UPDATE touch_plan
SET cited_facts = COALESCE((
    SELECT jsonb_agg(
        CASE WHEN jsonb_typeof(item) = 'object'
             THEN to_jsonb(item ->> 'text')
             ELSE item END
        ORDER BY position
    )
    FROM jsonb_array_elements(cited_facts) WITH ORDINALITY AS facts(item, position)
), '[]'::jsonb)
WHERE cited_facts IS NOT NULL AND jsonb_typeof(cited_facts) = 'array'
"""


def upgrade() -> None:
    op.execute(_TO_OBJECTS)
    op.bulk_insert(
        _AGENT_PROMPT_TABLE,
        [
            {
                "version": 1,
                "prompt_name": PROMPT_NAME,
                "template": PROMPT,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "status": "published",
                "published_at": _PUBLISHED_AT,
            }
        ],
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM agent_prompt WHERE prompt_name = '{PROMPT_NAME}'")
    op.execute(_TO_STRINGS)
