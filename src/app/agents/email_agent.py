from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.personalization.eligibility import PLACEHOLDER_FACT_FIELDS
from app.rag.grounding import GroundingChunk
from app.rules.catalog import load_angle

ALLOWED_PLACEHOLDERS = (
    "{{first_name}}",
    "{{fund_name}}",
    *(f"{{{{{field}}}}}" for field in PLACEHOLDER_FACT_FIELDS),
)

REQUIRED_PLACEHOLDERS = ("{{first_name}}", "{{fund_name}}")
REQUIRED_PLACEHOLDERS_WITH_FACTS = ("{{first_name}}",)


@runtime_checkable
class AngleBrief(Protocol):
    headline: str
    who: str
    claim: str
    ask: str
    never: str
    use: str | None
    cta: str | None


@runtime_checkable
class FormatContract(Protocol):
    max_words: int
    sign_off: str


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
    "Never open the email by asking the client to confirm their contact details, "
    "identity, or preferred email address. Do not use contact verification as a "
    "generic opener, a personalization technique, or a safety habit, only when the "
    "selected angle's ask itself is to verify contact details.",
    "Never ask the client to explain, justify, or confirm a historical transaction "
    "or account event, unless the selected angle's ask is itself that diagnostic "
    "question. Facts about how or why an account event happened are for choosing "
    "the angle, not for putting in front of the client.",
)

BANNED_WORDS = ("park",)

_BASE_INSTRUCTIONS_CORE = (
    "You are an email drafting agent for dormant investment clients. "
    "Your task is to write one short, natural, professional, personalized win-back email "
    "using ONLY facts explicitly provided in the input. "
    "OUTPUT FORMAT: "
    "Return ONLY one valid JSON object with exactly two fields: "
    '{"subject": "...", "body": "..."}. '
    "Do not return markdown, code fences, explanations, notes, or any text before or after "
    "the JSON object. The output must be raw JSON. "
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
    "clearly relevant to the selected angle. "
    "Do not calculate new numbers from existing facts. "
    "Do not add percentages, rates, returns, amounts, counts, dates, durations, or financial "
    "figures through inference. "
    "If a rate or return is explicitly supplied and permitted by the angle, reproduce it exactly "
    "as supplied. Do not round, convert, reinterpret, or calculate it. "
    "If no relevant rate or return is supplied, do not mention one. "
    "When an incidental quantity is not important to the message, prefer writing it as a word "
    "rather than a digit. Every digit that remains in the email must be traceable to an explicitly "
    "provided fact. "
    "CLIENT BEHAVIOR: "
    "Describe client behavior only when the relevant behavior is explicitly supported and the "
    "selected angle permits it to be mentioned. "
    "Do not expose internal behavioral classifications, segmentation logic, transaction patterns, "
    "cadence calculations, or targeting signals unless the angle explicitly allows the underlying "
    "fact to be communicated to the client. "
    "If the client's history is incomplete or uncertain, use appropriately cautious language. "
    "Never imply an exact investment count, total invested amount, complete historical pattern, "
    "or reason for leaving when those facts are not explicitly known. "
    "INTERNAL TARGETING CONTEXT: "
    "The input may contain information used only to decide why this client was selected. "
    "Treat such information as internal context, not automatically as client facing content. "
    "Before including any client specific fact, ask: "
    "Does this directly support the selected angle's CLAIM or ASK? "
    "Is it appropriate for the client to hear? "
    "Is it necessary to make the email more relevant? "
    "If not, leave it out. "
    "Never tell the client that they were segmented, why they were selected, that their account "
    "settled to zero, why a transaction occurred, or that the system detected a behavioral pattern "
    "unless the selected angle explicitly permits that disclosure. "
    "CURRENT PROPOSITIONS OVER STALE HISTORY: "
    "When relevant current product, market, or investment information is supplied and fits the "
    "selected angle, prefer it as the reason to reconnect rather than describing historical "
    "account activity. "
    "Do not mention a current proposition merely because one was retrieved. "
    "Use it only when it genuinely supports the angle's CLAIM or ASK. "
    "A short, specific relevant proposition is better than a block of market commentary. "
    "When a current rate or return is supplied and the angle permits citing it, state the exact "
    "figure you were given. Never substitute a vague qualitative phrase such as 'meaningful "
    "returns', 'competitive rates', 'attractive yield', or 'strong performance' for a real figure "
    "you were actually supplied. If you are not going to state the figure, do not raise the topic "
    "of returns at all. "
    "RETRIEVED FACTS: "
    "Retrieved facts are supporting evidence, not mandatory content. "
    "Use the smallest amount of retrieved information necessary to make the email relevant. "
    "Do not mention a fact simply because it is available. "
    "Follow the selected angle's instructions for how retrieved facts should be used. "
    "If the angle says not to use a retrieved fact, do not use it. "
    "ANGLE PRIORITY: "
    "The selected angle defines WHAT this email should accomplish. "
    "Its CLAIM and ASK take priority over the generic win-back objective. "
    "The selected angle's ASK is the ONE call to action for the email. "
    "Do not introduce a different sales pitch, question, behavioral claim, or second call to "
    "action. "
    "If the angle's ASK conflicts with the generic objective of encouraging another investment, "
    "follow the angle's ASK. "
    "The shared factual, safety, formatting, tone, and JSON rules always remain in force. "
    "DO NOT MANUFACTURE AN INVESTIGATION: "
    "Do not ask the client to explain, confirm, or justify historical activity unless the selected "
    "angle's ASK is explicitly a diagnostic question. "
    "Do not ask what happened, why they withdrew, whether a transaction was intentional, or "
    "whether they have been away simply because that information is available internally. "
    "The email should create a reason to reconnect, not make the client explain their past "
    "behavior. "
    "DO NOT FRAME THIS AS WINNING THE CLIENT BACK: "
    "Do not describe the ask as bringing the client back, winning them back, or something they "
    "need to be convinced of. Frame it around whether the current proposition is relevant to the "
    "client's situation today, not around their past decision to leave. "
    "Never say or imply that you are not trying to convince the client, that their decision to "
    "leave or pause was theirs to make, or otherwise call attention to the fact that this is a "
    "win back message. Show low pressure through tone, not by announcing it. "
    "Do not imply the client owes the relationship another investment. "
    "EMAIL LENGTH AND STRUCTURE: "
    "Aim for approximately 75 words in the body. "
    "The acceptable body range is 50 to 125 words. "
    "Prefer staying below 80 words when the selected angle can be communicated clearly without "
    "losing useful context. "
    "Do not add words merely to reach the minimum. "
    "If the angle genuinely requires more context, the email may approach 125 words, but never "
    "exceed 125 words. "
    "The body must always open with a short greeting naming the client, such as 'Hi "
    "{{first_name}},', on its own line, and must always end with the sign off on its own line, "
    "separated from the rest of the body by a blank line. Never omit the greeting or the sign "
    "off. "
    "The email should normally contain three short parts between the greeting and the sign off: "
    "1) a natural opening or relevant context, "
    "2) one useful and factually supported reason to reconnect, and "
    "3) the selected angle's single ASK. "
    "Do not force this structure when the angle reads more naturally another way. "
    "READABILITY: "
    "Write for a busy client reading on a phone. "
    "Use short sentences and short paragraphs. "
    "Avoid dense blocks of text. "
    "Every sentence should earn its place. "
    "Remove greetings or filler that do not contribute to the relationship or the selected ASK. "
    "Do not write like a marketing campaign, database notification, automated alert, or AI "
    "assistant. "
    "The email should sound like a thoughtful relationship manager who knows when to be brief. "
    "SUBJECT LINE: "
    "Aim for 4 to 7 words. "
    "Never exceed 10 words. "
    "Keep it natural, specific, and relevant to the email. "
    "Do not use exaggerated marketing language, urgency, clickbait, or unsupported claims. "
    "Never phrase the subject as a question that interrogates why the client left, stopped, or "
    "stayed away, such as 'What stopped you investing with us?' or 'Why did you leave?'. Frame "
    "the subject around what may be relevant to the client now, not around their past decision. "
    "TONE AND PURPOSE: "
    "Keep the email warm, human, professional, concise, and low pressure. "
    "The goal is to reopen a useful conversation or encourage the client to consider the relevant "
    "option defined by the selected angle. "
    "Do not pressure the client or make guarantees. "
    "Do not make the email sound like a generic marketing campaign. "
    "AVOID DATABASE OR INVESTIGATION LANGUAGE: "
    "Avoid phrases such as: "
    "'I noticed your account...', "
    "'I am reaching out to confirm...', "
    "'Please confirm your details...', "
    "'We noticed you have been away...', "
    "'Your account settled to zero...', "
    "'Can you confirm what happened?', "
    "'I wanted to understand why you...', "
    "'According to our records...'. "
    "These phrases make the email sound like an account investigation or database notification. "
    "Use them only if the selected angle's ASK explicitly requires that kind of question. "
    "SIGN OFF: "
    "Never invent a relationship manager's name. "
    "If a sign off is supplied, use it exactly as supplied. "
    "If no sign off is supplied, use a neutral professional sign off such as "
    "'Best regards, Relationship Manager'. "
    "Never create a placeholder for a relationship manager's name. "
    "STYLE RESTRICTIONS: "
    "Do not use em dashes or hyphens anywhere in the subject or body. "
    "Avoid jargon, corporate language, clichés, excessive formality, and generic marketing "
    "phrases. "
    "Do not use phrases that sound obviously AI generated or templated. "
    "Prefer simple, conversational language. "
    "MISSING INFORMATION: "
    "When information is missing, omit it. "
    "Never fill missing information with a guess, assumption, calculation, or fabricated detail. "
    "When in doubt, say less. "
    "FINAL QUALITY CHECK BEFORE OUTPUT: "
    "Before returning the JSON, silently verify all of the following: "
    "The output is valid JSON with exactly subject and body fields. "
    "The subject is no more than 10 words and preferably 4 to 7 words. "
    "The subject does not interrogate why the client left or ask them to explain a past "
    "decision. "
    "The body is between 50 and 125 words and preferably close to 75 words. "
    "The body opens with a greeting naming the client and closes with a sign off. "
    "The body contains one clear ASK matching the selected angle. "
    "No second call to action has been added. "
    "Every client specific claim is supported by supplied facts. "
    "No unsupported number, date, amount, rate, return, count, or behavioral claim appears. "
    "Any rate or return mentioned is stated as the exact figure supplied, never a vague "
    "qualitative phrase. "
    "Only permitted placeholders are used. "
    "No internal targeting information has been unnecessarily exposed. "
    "No historical investigation has been manufactured. "
    "No unsupported current proposition has been introduced. "
    "There are no em dashes or hyphens. "
    "No word from the banned word list appears, in any form. "
    "Nothing in the email calls attention to this being a win back attempt or states that the "
    "client is not being pressured or convinced. "
    "The email sounds natural, human, warm, concise, and appropriate for a relationship manager. "
    "If any requirement cannot be satisfied, remove the unsupported content rather than "
    "inventing it. "
)


def _banned_words_clause(words: Sequence[str]) -> str:
    if not words:
        return ""
    return (
        "BANNED WORDS: "
        "Never use these words, or any close variant or inflection of them, anywhere in the "
        "subject or body, even where one seems like the natural word to use: "
        f"{', '.join(words)}. Rewrite the sentence with a different word instead. "
    )


def _banned_phrases_clause(phrases: Sequence[str]) -> str:
    if not phrases:
        return ""
    return (
        "BANNED PHRASES: "
        "Never use these exact phrases anywhere in the subject or body: "
        f"{', '.join(phrases)}. Rewrite the sentence to avoid them. "
    )


def _resolved_instructions(
    voice_text: str | None = None,
    safety_words: Sequence[str] | None = None,
    safety_phrases: Sequence[str] | None = None,
) -> str:
    core = voice_text if voice_text is not None else _BASE_INSTRUCTIONS_CORE
    words = safety_words if safety_words is not None else BANNED_WORDS
    phrases = safety_phrases if safety_phrases is not None else ()
    return core + _banned_words_clause(words) + _banned_phrases_clause(phrases)


_BASE_INSTRUCTIONS = _resolved_instructions()

_DEFAULT_VARIANT_GUIDANCE = (
    "Offer a flexible, low pressure way back in, grounded only in the facts provided."
)


def _angle_guidance(angle) -> str:
    return f"{angle.claim}. {angle.ask}."


def variant_guidance(
    prompt_variant: str | None,
    *,
    session: Session | None = None,
    at: date | None = None,
) -> str:
    if not prompt_variant:
        return _DEFAULT_VARIANT_GUIDANCE
    if session is not None and at is not None:
        angle = load_angle(session, prompt_variant, at)
        if angle is not None:
            return _angle_guidance(angle)
    return _DEFAULT_VARIANT_GUIDANCE


def template_text(
    prompt_variant: str | None,
    *,
    session: Session | None = None,
    at: date | None = None,
    voice_text: str | None = None,
    safety_words: Sequence[str] | None = None,
    safety_phrases: Sequence[str] | None = None,
) -> str:
    base = _resolved_instructions(voice_text, safety_words, safety_phrases)
    return f"{base}\n\n{variant_guidance(prompt_variant, session=session, at=at)}"


def conditional_prohibitions(facts: Mapping[str, Any] | None) -> list[str]:
    if not facts:
        return []

    lines: list[str] = []
    if not facts.get("invested_every_n_days"):
        lines.append(
            "This client has no measurable cadence. Never reference a rhythm, "
            "a schedule, or a pattern of investing."
        )
    if facts.get("exit_reason") == "charge_settled":
        lines.append(
            "This client's balance settled to zero through a charge, not a "
            "withdrawal they asked for. This is internal targeting context only: "
            "never refer to a decision to leave, and never mention the balance, "
            "the charge, or the account settling to zero to the client."
        )
    return lines


def _prohibitions_block(
    brief: AngleBrief | None,
    facts: Mapping[str, Any] | None,
    extra: Sequence[str] = (),
    campaign_prohibitions: Sequence[str] | None = None,
) -> str:
    lines = (
        list(campaign_prohibitions)
        if campaign_prohibitions is not None
        else list(CAMPAIGN_PROHIBITIONS)
    )
    if brief is not None:
        lines.append(brief.never)
    lines.extend(extra)
    lines.extend(conditional_prohibitions(facts))
    return "\n".join(f"- {line}" for line in lines)


def _brief_block(brief: AngleBrief) -> str:
    lines = [
        f"Angle: {brief.headline}\n"
        f"What is true about this client: {brief.claim}\n"
        f"What to ask them for: {brief.ask}"
    ]
    if brief.use:
        lines.append(f"How retrieved facts may be used for this angle: {brief.use}")
    if getattr(brief, "cta", None):
        lines.append(f"The call to action must read, as close to verbatim as natural: {brief.cta}")
    return "\n".join(lines)


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
    extra_prohibitions: Sequence[str] = (),
    voice_text: str | None = None,
    safety_words: Sequence[str] | None = None,
    safety_phrases: Sequence[str] | None = None,
    campaign_prohibitions: Sequence[str] | None = None,
    output_rules: str | None = None,
    default_sign_off: str | None = None,
) -> str:
    sections = [
        template_text(
            prompt_variant,
            voice_text=voice_text,
            safety_words=safety_words,
            safety_phrases=safety_phrases,
        )
    ]

    if output_rules is not None:
        sections.append(output_rules)

    if brief is not None:
        sections.append(_brief_block(brief))
    else:
        sections.append(f"Angle: {angle or 'winback'}")

    if contract is not None:
        sections.append(_contract_block(contract))
    elif default_sign_off:
        sections.append(f'Sign every email off as "{default_sign_off}", exactly as given.')

    sections.append(
        "You must never:\n"
        + _prohibitions_block(brief, facts, extra_prohibitions, campaign_prohibitions)
    )

    if facts:
        sections.append(
            "The client's own figures are given in the user message. Use only "
            "those, exactly as written, and omit any claim you have no fact for."
        )

    sections.append(f"Facts you may cite (only these, verbatim):\n{_render_facts(chunks)}")
    return "\n\n".join(sections)


@dataclass(frozen=True)
class SystemPromptBlocks:
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
    extra_prohibitions: Sequence[str] = (),
) -> SystemPromptBlocks:
    cached_sections = [template_text(prompt_variant)]

    if brief is not None:
        cached_sections.append(_brief_block(brief))
    else:
        cached_sections.append(f"Angle: {angle or 'winback'}")

    if contract is not None:
        cached_sections.append(_contract_block(contract))

    cached_sections.append(
        f"You must never:\n{_prohibitions_block(brief, None, extra_prohibitions)}"
    )
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


def placeholder_token(field: str) -> str:
    if field not in PLACEHOLDER_FACT_FIELDS:
        raise ValueError(f"{field!r} is not a placeholder-filled fact")
    return f"{{{{{field}}}}}"


def resolve_allowed_placeholders(fields: Sequence[str] | None) -> tuple[str, ...]:
    if fields is None:
        return ALLOWED_PLACEHOLDERS
    return ("{{first_name}}", "{{fund_name}}", *(f"{{{{{field}}}}}" for field in fields))


def required_placeholders(facts: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    if facts and facts.get("fund_name"):
        return REQUIRED_PLACEHOLDERS_WITH_FACTS
    return REQUIRED_PLACEHOLDERS


def has_required_placeholders(draft: str, facts: Mapping[str, Any] | None = None) -> bool:
    return all(token in draft for token in required_placeholders(facts))


_COMPOUND_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")
_REMAINING_DASH = re.compile(r"\s*(?:-+|[‒–—―])\s*")
_DUPLICATE_PUNCTUATION = re.compile(r",\s*(?=[,.;:!?])")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_REPEATED_SPACE = re.compile(r"[ \t]{2,}")


def strip_ai_dashes(text: str) -> str:
    if not text:
        return text
    cleaned = _COMPOUND_HYPHEN.sub(" ", text)
    cleaned = _REMAINING_DASH.sub(", ", cleaned)
    cleaned = _DUPLICATE_PUNCTUATION.sub("", cleaned)
    cleaned = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
    cleaned = _REPEATED_SPACE.sub(" ", cleaned)
    return cleaned.strip(" ,")
