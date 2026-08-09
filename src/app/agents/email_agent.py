"""EmailAgent: one draft prompt assembled from three interchangeable slots.

The angle brief says what may be claimed and asked for, the format contract
says how long it runs and who signs it, and the prohibitions say what may
never be said. The client's own figures are not here: they travel as the
scanned payload, so nothing client-specific reaches the model unchecked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.rag.grounding import GroundingChunk
from app.rules.catalog import load_angle

# The only tokens a draft may use for anything client specific.
REQUIRED_PLACEHOLDERS = ("{{first_name}}", "{{fund_name}}")
# A draft given the fund name as a fact writes it directly, so only the name
# the model never sees stays a placeholder.
REQUIRED_PLACEHOLDERS_WITH_FACTS = ("{{first_name}}",)


@runtime_checkable
class AngleBrief(Protocol):
    """The catalogue row for one angle."""

    headline: str
    who: str
    claim: str
    ask: str
    never: str


@runtime_checkable
class FormatContract(Protocol):
    """The tier row saying how a message on this tier is shaped."""

    max_words: int
    sign_off: str


# Derived from the properties of the source data, not from taste. These are
# claims the extract cannot support, so they ride on every prompt.
CAMPAIGN_PROHIBITIONS = (
    "Never state how many times the client invested. Only part of their history "
    "is visible, so any count would be wrong for much of this population.",
    "Never mention a balance, an amount still invested, or money waiting in an "
    "account. Every client here holds none.",
    "Never imply when the client first invested, or how long the relationship "
    "lasted in total. Only their recent activity is visible.",
    "Never state a number that is not in the facts you were given. Do not "
    "calculate, round, or combine them into a new one.",
    "Never promise a return, a rate, or a guarantee.",
)

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
    "Write any incidental quantity as a word rather than a digit, so that "
    "every digit in the message traces back to a fact you were given. "
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

# The one-vocabulary default: every prompt_variant is a v3 angle identifier
# now, so its guidance always comes from the catalogue. This is only what a
# variant with no catalogue entry falls back to, not a per-variant lookup.
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

    A prompt variant is a v3 angle identifier, so this resolves against the
    active angle catalogue when a session and a reference date are given.
    Without them, or when the catalogue carries no such angle, this returns
    the one generic default rather than a fixed per-variant dictionary.
    """
    if not prompt_variant:
        return _DEFAULT_VARIANT_GUIDANCE
    if session is not None and at is not None:
        angle = load_angle(session, prompt_variant, at)
        if angle is not None:
            return _angle_guidance(angle)
    return _DEFAULT_VARIANT_GUIDANCE


def template_text(prompt_variant: str | None) -> str:
    """The stable instructions for a prompt variant: base contract plus framing.

    This excludes the angle line and retrieved facts, since those are data,
    not template; llmops.versions hashes exactly this to register a
    prompt_versions row, so a call with unchanged wording reuses the same
    version and a genuine wording change registers a new one.
    """
    return f"{_BASE_INSTRUCTIONS}\n\n{variant_guidance(prompt_variant)}"


def conditional_prohibitions(facts: Mapping[str, Any] | None) -> list[str]:
    """The prohibitions this one client's own situation adds.

    Each is driven by a fact the schema already decided: an absent cadence
    fact means the client demonstrably has no rhythm to reference.
    """
    if not facts:
        return []

    lines: list[str] = []
    if not facts.get("invested_every_n_days"):
        lines.append(
            "This client has no measurable cadence. Never reference a rhythm, "
            "a schedule, or a pattern of investing."
        )
    if facts.get("stale_contact"):
        lines.append(
            "Contact details for this client are over three years old. Open by "
            "confirming you have reached the right person."
        )
    if facts.get("exit_reason") == "charge_settled":
        lines.append(
            "This client's balance settled to zero through a charge, not a "
            "withdrawal they asked for. Never refer to a decision to leave."
        )
    return lines


def _prohibitions_block(brief: AngleBrief | None, facts: Mapping[str, Any] | None) -> str:
    lines = [*CAMPAIGN_PROHIBITIONS]
    if brief is not None:
        lines.append(brief.never)
    lines.extend(conditional_prohibitions(facts))
    return "\n".join(f"- {line}" for line in lines)


def _brief_block(brief: AngleBrief) -> str:
    return (
        f"Angle: {brief.headline}\n"
        f"What is true about this client: {brief.claim}\n"
        f"What to ask them for: {brief.ask}"
    )


def _contract_block(contract: FormatContract) -> str:
    return (
        f"Write no more than {contract.max_words} words in the body. "
        f"Sign the message off as {contract.sign_off}."
    )


def _render_facts(chunks: Sequence[GroundingChunk]) -> str:
    if not chunks:
        return "(no facts retrieved; do not cite a rate or return)"
    return "\n".join(f"- {chunk.text}" for chunk in chunks)


def build_system_prompt(
    *,
    angle: str | None,
    prompt_variant: str | None,
    chunks: Sequence[GroundingChunk] = (),
    brief: AngleBrief | None = None,
    contract: FormatContract | None = None,
    facts: Mapping[str, Any] | None = None,
) -> str:
    """The full system prompt for one draft.

    brief, contract and facts are the three slots. Without them this returns
    exactly the prompt it always did, so a caller that predates the catalogue
    keeps working. facts is read only to decide which prohibitions apply; the
    figures themselves reach the model as the scanned payload, never here.
    """
    sections = [template_text(prompt_variant)]

    if brief is not None:
        sections.append(_brief_block(brief))
    else:
        sections.append(f"Angle: {angle or 'winback'}")

    if contract is not None:
        sections.append(_contract_block(contract))

    sections.append(f"You must never:\n{_prohibitions_block(brief, facts)}")

    if facts:
        sections.append(
            "The client's own figures are given in the user message. Use only "
            "those, exactly as written, and omit any claim you have no fact for."
        )

    sections.append(f"Facts you may cite (only these, verbatim):\n{_render_facts(chunks)}")
    return "\n\n".join(sections)


@dataclass(frozen=True)
class SystemPromptBlocks:
    """The same prompt build_system_prompt assembles, cut at the boundary
    between what every client sharing this angle, tier, and product sees
    identically, and what this one client's own facts add.

    cached is everything determined by (angle, tier, product): the base
    instructions, the angle's brief, the tier's format contract, the
    campaign- and angle-level prohibitions, and the retrieved chunks --
    none of it varies for another client on the same angle, tier and
    product. dynamic is this client's own conditional prohibitions (no
    measurable cadence, stale contact, a charge-settled exit) plus the note
    that their figures follow in the user turn; both depend on this one
    client's facts and would break a cache hit if folded into cached.
    """

    cached: str
    dynamic: str


def build_system_prompt_blocks(
    *,
    angle: str | None,
    prompt_variant: str | None,
    chunks: Sequence[GroundingChunk] = (),
    brief: AngleBrief | None = None,
    contract: FormatContract | None = None,
    facts: Mapping[str, Any] | None = None,
) -> SystemPromptBlocks:
    """build_system_prompt, split for prompt caching rather than joined.

    Written for campaigns.batch_generation: the Message Batches API caches
    per request, on a marked content block, so many clients' requests only
    earn a cache hit if the cached half is byte-for-byte identical across
    them. Concatenating cached and dynamic (in that order, with a blank
    line between) covers the same ground build_system_prompt does; the
    client-specific clauses move to the end instead of sitting between the
    prohibitions and the facts block, which changes nothing the model is
    told, only where in the prompt it is told.
    """
    cached_sections = [template_text(prompt_variant)]

    if brief is not None:
        cached_sections.append(_brief_block(brief))
    else:
        cached_sections.append(f"Angle: {angle or 'winback'}")

    if contract is not None:
        cached_sections.append(_contract_block(contract))

    # facts=None here on purpose: the campaign- and angle-level prohibitions
    # only, so this block stays identical for every client on this angle
    # and tier. This client's own conditional prohibitions go in dynamic.
    cached_sections.append(f"You must never:\n{_prohibitions_block(brief, None)}")
    cached_sections.append(f"Facts you may cite (only these, verbatim):\n{_render_facts(chunks)}")

    dynamic_sections = []
    conditional = conditional_prohibitions(facts)
    if conditional:
        dynamic_sections.append(
            "This client also must never:\n" + "\n".join(f"- {line}" for line in conditional)
        )
    if facts:
        dynamic_sections.append(
            "The client's own figures are given in the user message. Use only "
            "those, exactly as written, and omit any claim you have no fact for."
        )

    return SystemPromptBlocks(
        cached="\n\n".join(cached_sections), dynamic="\n\n".join(dynamic_sections)
    )


def render_call_brief(
    *,
    brief: AngleBrief,
    facts: Mapping[str, Any],
    contract: FormatContract | None = None,
) -> str:
    """The top tier's call brief, rendered from the same inputs as its email.

    Not a second generation: no model call, no second set of claims. It is
    written for the relationship manager, so it has no subject line and names
    the client only as the placeholder the delivery layer resolves.
    """
    lines = [
        f"Call brief: {brief.headline}",
        "",
        f"Who this is: {brief.who}",
        f"What is true about them: {brief.claim}",
        f"What to ask for: {brief.ask}",
        "",
        "What you must never say:",
        _prohibitions_block(brief, facts),
        "",
        "What we know about them:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(facts.items()))
    if contract is not None:
        lines.extend(["", f"Call as: {contract.sign_off}."])
    return "\n".join(lines)


def required_placeholders(facts: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """The placeholders a draft must still carry, given what it was told.

    A draft handed the fund name as a fact writes it directly, so only the
    client's name, which the model never sees, has to stay a token.
    """
    if facts and facts.get("fund_name"):
        return REQUIRED_PLACEHOLDERS_WITH_FACTS
    return REQUIRED_PLACEHOLDERS


def has_required_placeholders(draft: str, facts: Mapping[str, Any] | None = None) -> bool:
    """True when every placeholder this draft still needs appears in it."""
    return all(token in draft for token in required_placeholders(facts))
