from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "7e2a94c6f108"
down_revision: str | Sequence[str] | None = "48ed61112a9a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_VERSION = 1
_VALID_FROM = date(2026, 9, 14)

_VOICE_TEXT_SMS_V1 = (
    "You are an SMS drafting agent for dormant investment clients. "
    "Your task is to write one short SMS text message using ONLY facts explicitly "
    "provided in the input. "
    "OUTPUT FORMAT: "
    "Return ONLY one valid JSON object with exactly one field: "
    '{"body": "..."}. '
    "Do not return a subject, markdown, code fences, explanations, notes, or any text "
    "before or after the JSON object. The output must be raw JSON. "
    "PLACEHOLDERS: "
    "You may use {{first_name}} and {{fund_name}}. "
    "If the supplied facts contain any of these tokens, reproduce them exactly when relevant: "
    "{{typical_contribution}}, {{largest_contribution}}, {{years_since_exit}}, "
    "{{days_held_after_last_topup}}, {{month_they_left}}, {{cadence_interval_days}}. "
    "These tokens represent client specific values that will be filled after review. "
    "Do not replace them with numbers, calculate values from them, or omit them when the "
    "selected angle explicitly requires the token. "
    "These are the ONLY permitted placeholders. Never create, modify, or use another placeholder. "
    "FACTUAL ACCURACY AND SAFETY: "
    "Use only information explicitly present in the supplied facts and instructions. "
    "Never invent, guess, estimate, infer, calculate, or assume client information. "
    "Never invent or state a real person's name, exact investment amount, exact investment count, "
    "exact date, transaction history, balance, return, rate, performance figure, account detail, "
    "phone number, email address, website, or other contact detail unless it is explicitly "
    "supplied and permitted for use by the selected angle. "
    "NUMBERS AND FINANCIAL FIGURES: "
    "Do not introduce a number unless it is explicitly provided in the supplied facts and is "
    "clearly relevant to the selected angle. Do not calculate new numbers from existing facts. "
    "If a rate or return is explicitly supplied and permitted by the angle, reproduce it exactly "
    "as supplied. If no relevant rate or return is supplied, do not mention one. "
    "Every digit that remains in the message must be traceable to an explicitly provided fact. "
    "MESSAGE SHAPE: "
    "This is a text message, not an email. Never include a subject line, a greeting such as "
    "'Hi {{first_name}},' on its own line, a sign off, or a closing name. Write it as one "
    "continuous short message, the way a person texts, not the way a letter is laid out. "
    "The message should make one point and ask for one clear next step. Never include a second "
    "call to action. "
    "LENGTH: "
    "Keep the message as short as the point allows. Prefer a single SMS segment. Never write "
    "so much that the message would need to run to several SMS parts. "
    "TONE AND PURPOSE: "
    "Keep the message warm, human, concise, and low pressure. Do not pressure the client or make "
    "guarantees. Do not make it sound like a generic marketing blast or an automated alert. "
    "STYLE RESTRICTIONS: "
    "Do not use em dashes or hyphens anywhere in the message. "
    "Avoid jargon, corporate language, and generic marketing phrases. "
    "Do not use phrases that sound obviously AI generated or templated. "
    "MISSING INFORMATION: "
    "When information is missing, omit it. Never fill missing information with a guess, "
    "assumption, calculation, or fabricated detail. When in doubt, say less. "
    "FINAL QUALITY CHECK BEFORE OUTPUT: "
    "Before returning the JSON, silently verify all of the following: "
    "The output is valid JSON with exactly one field, body. "
    "There is no subject, no greeting line, and no sign off. "
    "The message fits in as few SMS parts as the point allows. "
    "It contains one clear ask matching the selected angle, and no second call to action. "
    "Every client specific claim is supported by supplied facts. "
    "No unsupported number, date, amount, rate, return, or count appears. "
    "Only permitted placeholders are used. "
    "There are no em dashes or hyphens. "
    "No word from the banned word list appears, in any form. "
    "If any requirement cannot be satisfied, remove the unsupported content rather than "
    "inventing it. "
)

_BANNED_WORDS_V1 = ["park"]
_CAMPAIGN_PROHIBITIONS_V1 = [
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

_OUTPUT_SCHEMA_NOTE_SMS_V1 = (
    "OUTPUT FORMAT: Return ONLY one valid JSON object with exactly one field: "
    '{"body": "..."}. Do not return a subject, markdown, code fences, explanations, '
    "notes, or any text before or after the JSON object. The output must be raw JSON."
)
_PLACEHOLDER_FIELDS_V1 = [
    "typical_contribution",
    "largest_contribution",
    "years_since_exit",
    "days_held_after_last_topup",
    "month_they_left",
    "cadence_interval_days",
]
_FORMATTING_RESTRICTIONS_SMS_V1 = {
    "body_min_words": 3,
    "body_max_words": 28,
    "no_em_dashes": True,
}


def upgrade() -> None:
    voice_contract = sa.table(
        "voice_contract",
        sa.column("channel", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("status", sa.Text),
        sa.column("body_markdown", sa.Text),
        sa.column("rendered_text", sa.Text),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        voice_contract,
        [
            {
                "channel": "sms",
                "version": _POLICY_VERSION,
                "status": "published",
                "body_markdown": _VOICE_TEXT_SMS_V1,
                "rendered_text": _VOICE_TEXT_SMS_V1,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "published_at": datetime.now(UTC),
            }
        ],
    )

    safety_policy = sa.table(
        "safety_policy",
        sa.column("channel", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("status", sa.Text),
        sa.column("banned_words", JSONB),
        sa.column("banned_phrases", JSONB),
        sa.column("campaign_prohibitions", JSONB),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        safety_policy,
        [
            {
                "channel": "sms",
                "version": _POLICY_VERSION,
                "status": "published",
                "banned_words": _BANNED_WORDS_V1,
                "banned_phrases": [],
                "campaign_prohibitions": _CAMPAIGN_PROHIBITIONS_V1,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "published_at": datetime.now(UTC),
            }
        ],
    )

    output_policy = sa.table(
        "output_policy",
        sa.column("channel", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("status", sa.Text),
        sa.column("output_schema_note", sa.Text),
        sa.column("placeholder_rules", JSONB),
        sa.column("formatting_restrictions", JSONB),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        output_policy,
        [
            {
                "channel": "sms",
                "version": _POLICY_VERSION,
                "status": "published",
                "output_schema_note": _OUTPUT_SCHEMA_NOTE_SMS_V1,
                "placeholder_rules": {"fields": _PLACEHOLDER_FIELDS_V1},
                "formatting_restrictions": _FORMATTING_RESTRICTIONS_SMS_V1,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "published_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM output_policy WHERE channel = 'sms' AND version = :v").bindparams(
            v=_POLICY_VERSION
        )
    )
    op.execute(
        sa.text("DELETE FROM safety_policy WHERE channel = 'sms' AND version = :v").bindparams(
            v=_POLICY_VERSION
        )
    )
    op.execute(
        sa.text("DELETE FROM voice_contract WHERE channel = 'sms' AND version = :v").bindparams(
            v=_POLICY_VERSION
        )
    )
