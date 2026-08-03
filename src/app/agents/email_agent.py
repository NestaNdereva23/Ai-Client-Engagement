"""EmailAgent: turns a client's rule outcome into a placeholder only draft prompt.

Prompt variant selection comes straight from the resolved business rule(Business Rule Module),
carried as GenerationState.prompt_variant"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from sqlalchemy.orm import Session

from app.rag.grounding import GroundingChunk
from app.rules.catalog import load_angle

# The only tokens a draft may use for anything client specific.
REQUIRED_PLACEHOLDERS = ("{{first_name}}", "{{fund_name}}")

_BASE_INSTRUCTIONS = (
    "You are an email drafting agent for dormant investment clients. "
    "Your task is to write one short, professional, personalized win-back email "
    "using ONLY the facts explicitly provided in the input. "
    "OUTPUT FORMAT: "
    "Return ONLY one valid JSON object with exactly two fields: "
    '{"subject": "...", "body": "..."}. '
    "Do not return markdown, code fences, explanations, notes, or any text "
    "before or after the JSON object. Here is an example of the correct raw output format:\n"
    '{"subject": "....", "body": "...."}'
    "avoid "
    '```json{"subject": "....", "body": "...."}\n```'
    "PLACEHOLDERS: "
    "You may use {{first_name}} for the client's name and {{fund_name}} "
    "for the fund or product name. "
    "These are the ONLY placeholders allowed. "
    "Do not create, modify, or use any other placeholder. "
    "FACTUAL ACCURACY: "
    "Use only information explicitly present in the provided facts and "
    "instructions. Do not invent, guess, estimate, calculate, infer, or assume "
    "any client information. "
    "Never invent or state a real person's name, exact investment amount, "
    "exact number of investments, exact date, transaction history, balance, "
    "return, rate, performance figure, account detail, phone number, email "
    "address, website, or other contact detail. "
    "NUMBERS AND FINANCIAL FIGURES: "
    "Do not include any number unless that number is explicitly provided in "
    "the facts supplied to you and is clearly relevant to the email. "
    "Do not calculate new numbers from provided facts. "
    "Do not add percentages, rates, returns, amounts, counts, dates, or "
    "financial figures based on assumptions. "
    "If a rate or return is provided, reproduce it exactly as provided; "
    "do not rephrase, round, convert, or calculate it. "
    "If no rate or return is provided, do not mention one. "
    "CLIENT BEHAVIOR: "
    "Only describe the client's investment behavior using the specific "
    "behavioral classification and guidance provided by the system. "
    "Do not add behavioral details that are not explicitly supported. "
    "For example, do not claim that a client invested a specific number of "
    "times, invested a specific amount, invested on a specific date, or had "
    "a specific investment pattern unless that exact information is provided "
    "and explicitly permitted by the guidance. "
    "SOFT CLASSIFICATIONS: "
    "If the guidance says the client's investment history is not completely "
    "known, do not state or imply an exact investment count, exact total, "
    "exact amount, or complete historical pattern. "
    "Use cautious language that reflects the uncertainty. "
    "PERSONALIZATION: "
    "Personalize only from the facts provided. "
    "The email should feel relevant but must remain factually grounded. "
    "Do not manufacture personalization. "
    "TONE AND PURPOSE: "
    "Keep the email short, warm, professional, and low pressure. "
    "The goal is to encourage the client to consider investing again or "
    "exploring their options, without making guarantees or unsupported claims. "
    "Do not pressure the client. "
    "IMPORTANT: "
    "When information is missing, omit it. Never fill missing information "
    "with a guess or fabricated detail. "
    "When in doubt, say less rather than inventing information."
)

# One line of tone and framing per prompt variant, keyed by the exact string a
# business rule resolves to. A rule set that names its angle in the catalogue
# reads its guidance from there instead; this dictionary is what an older rule
# set, or a lookup with no catalogue entry, falls back to.
_VARIANT_GUIDANCE: Mapping[str, str] = {
    "habit_premium": (
        "This client invested frequently and at a high value. Acknowledge "
        "the strong habit they built and invite them to resume it."
    ),
    "habit_standard": (
        "This client invested frequently. Invite them to resume their regular investing rhythm."
    ),
    "habit_premium_soft": (
        "This client invested frequently at a high value, though their full "
        "purchase history is not completely known to us. Invite them to "
        "resume investing without stating or implying an exact count or total."
    ),
    "habit_standard_soft": (
        "This client invested frequently, though their full purchase history "
        "is not completely known to us. Invite them to resume investing "
        "without stating or implying an exact count or total."
    ),
    "flexible_premium": (
        "This client invested once, at a high value. Offer a flexible, "
        "low pressure way back in that respects their pace."
    ),
    "flexible_standard": (
        "This client has invested occasionally. Offer a flexible, low pressure way back in."
    ),
    "flexible_minimal": (
        "We have no observed investment activity for this client yet. Keep "
        "the invitation light and exploratory, not a win back for a past habit."
    ),
    "flexible_soft": (
        "This client's investment history is not completely known to us. "
        "Offer a flexible, low pressure way back in without stating or "
        "implying an exact count or total."
    ),
}
_DEFAULT_VARIANT_GUIDANCE = (
    "Offer a flexible, low pressure way back in, grounded only in the facts provided."
)


def _angle_guidance(angle) -> str:
    """One line of tone and framing built from an angle's brief."""
    return f"{angle.claim}. {angle.ask}."


def variant_guidance(
    prompt_variant: str | None,
    *,
    session: Session | None = None,
    at: date | None = None,
) -> str:
    """The tone and framing line for a prompt variant, or a safe default.

    A prompt variant set to an angle identifier resolves against the active
    angle catalogue when a session and a reference date are given. Without
    them, or when the catalogue carries no such angle, this falls back to the
    fixed dictionary above, so a caller that predates the catalogue keeps
    working exactly as before.
    """
    if not prompt_variant:
        return _DEFAULT_VARIANT_GUIDANCE
    if session is not None and at is not None:
        angle = load_angle(session, prompt_variant, at)
        if angle is not None:
            return _angle_guidance(angle)
    return _VARIANT_GUIDANCE.get(prompt_variant, _DEFAULT_VARIANT_GUIDANCE)


def template_text(prompt_variant: str | None) -> str:
    """The stable instructions for a prompt variant: base contract plus framing.

    This excludes the angle line and retrieved facts, since those are data,
    not template; llmops.versions hashes exactly this to register a
    prompt_versions row, so a call with unchanged wording reuses the same
    version and a genuine wording change registers a new one.
    """
    return f"{_BASE_INSTRUCTIONS}\n\n{variant_guidance(prompt_variant)}"


def _render_facts(chunks: Sequence[GroundingChunk]) -> str:
    if not chunks:
        return "(no facts retrieved; do not cite a rate or return)"
    return "\n".join(f"- {chunk.text}" for chunk in chunks)


def build_system_prompt(
    *,
    angle: str | None,
    prompt_variant: str | None,
    chunks: Sequence[GroundingChunk] = (),
) -> str:
    """The full system prompt for one draft: the template, the angle, and the facts."""
    return (
        f"{template_text(prompt_variant)}\n\n"
        f"Angle: {angle or 'winback'}\n\n"
        f"Facts you may cite (only these, verbatim):\n{_render_facts(chunks)}"
    )


def has_required_placeholders(draft: str) -> bool:
    """True when every required placeholder token appears in the draft.
    A pure structural check, not a guardrail on its own
    """
    return all(token in draft for token in REQUIRED_PLACEHOLDERS)
