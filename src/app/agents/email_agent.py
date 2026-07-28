"""EmailAgent: turns a client's rule outcome into a placeholder only draft prompt.

Prompt variant selection comes straight from the resolved business rule(Business Rule Module),
carried as GenerationState.prompt_variant"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.rag.grounding import GroundingChunk

# The only tokens a draft may use for anything client specific.
REQUIRED_PLACEHOLDERS = ("{{first_name}}", "{{fund_name}}")

_BASE_INSTRUCTIONS = (
    "Draft a short win back email for a dormant investment client. "
    "Use {{first_name}} for the client's name and {{fund_name}} for the fund "
    "or product name; use no other placeholder token. Never invent or state a "
    "real name, exact amount, exact date, or contact detail. Only cite a rate "
    "or return that appears, verbatim, in the facts provided."
)

# One line of tone and framing per prompt variant, keyed by the exact string a
# business rule resolves to (rules/store.py, seeded in the business_rules
# migrations). An unknown variant, from a future rule version, falls back to
# _DEFAULT_VARIANT_GUIDANCE rather than erroring, so a new rule ships without
# a matching code change here.
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


def variant_guidance(prompt_variant: str | None) -> str:
    """The tone and framing line for a prompt variant, or a safe default."""
    if not prompt_variant:
        return _DEFAULT_VARIANT_GUIDANCE
    return _VARIANT_GUIDANCE.get(prompt_variant, _DEFAULT_VARIANT_GUIDANCE)


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
    """The full system prompt for one draft: the shared contract, plus the
    variant's framing, plus the facts the model may cite.
    """
    return (
        f"{_BASE_INSTRUCTIONS}\n\n"
        f"Angle: {angle or 'winback'}\n"
        f"{variant_guidance(prompt_variant)}\n\n"
        f"Facts you may cite (only these, verbatim):\n{_render_facts(chunks)}"
    )


def has_required_placeholders(draft: str) -> bool:
    """True when every required placeholder token appears in the draft.
    A pure structural check, not a guardrail on its own
    """
    return all(token in draft for token in REQUIRED_PLACEHOLDERS)
