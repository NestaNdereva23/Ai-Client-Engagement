from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.agents.email_agent import (
    BANNED_WORDS,
    CAMPAIGN_PROHIBITIONS,
    AngleBrief,
    FormatContract,
    banned_phrases_clause,
    banned_words_clause,
    brief_block,
    prohibitions_block,
    render_facts,
    variant_guidance,
)
from app.rag.grounding import GroundingChunk

_BASE_INSTRUCTIONS_CORE = (
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


def _resolved_instructions(
    voice_text: str | None = None,
    safety_words: Sequence[str] | None = None,
    safety_phrases: Sequence[str] | None = None,
    base_instructions: str | None = None,
) -> str:
    core = voice_text if voice_text is not None else base_instructions or _BASE_INSTRUCTIONS_CORE
    words = safety_words if safety_words is not None else BANNED_WORDS
    phrases = safety_phrases if safety_phrases is not None else ()
    return core + banned_words_clause(words) + banned_phrases_clause(phrases)


def template_text(
    prompt_variant: str | None,
    *,
    session: Session | None = None,
    at: date | None = None,
    voice_text: str | None = None,
    safety_words: Sequence[str] | None = None,
    safety_phrases: Sequence[str] | None = None,
    base_instructions: str | None = None,
) -> str:
    base = _resolved_instructions(voice_text, safety_words, safety_phrases, base_instructions)
    return f"{base}\n\n{variant_guidance(prompt_variant, session=session, at=at)}"


def build_sms_system_prompt(
    *,
    angle: str | None,
    prompt_variant: str | None = None,
    chunks: Sequence[GroundingChunk] = (),
    brief: AngleBrief | None = None,
    contract: FormatContract | None = None,
    facts: Mapping[str, Any] | None = None,
    extra_prohibitions: Sequence[str] = (),
    voice_text: str | None = None,
    safety_words: Sequence[str] | None = None,
    safety_phrases: Sequence[str] | None = None,
    campaign_prohibitions: Sequence[str] | None = None,
    output_rules: str | None = None,
    default_sign_off: str | None = None,
    base_instructions: str | None = None,
) -> str:
    sections = [_resolved_instructions(voice_text, safety_words, safety_phrases, base_instructions)]

    if output_rules is not None:
        sections.append(output_rules)

    if brief is not None:
        sections.append(brief_block(brief))
    else:
        sections.append(f"Angle: {angle or 'winback'}")

    sections.append(
        "You must never:\n"
        + prohibitions_block(
            brief,
            facts,
            extra_prohibitions,
            campaign_prohibitions if campaign_prohibitions is not None else CAMPAIGN_PROHIBITIONS,
        )
    )

    if facts:
        sections.append(
            "The client's own figures are given in the user message. Use only "
            "those, exactly as written, and omit any claim you have no fact for."
        )

    sections.append(f"Facts you may cite (only these, verbatim):\n{render_facts(chunks)}")
    return "\n\n".join(sections)
