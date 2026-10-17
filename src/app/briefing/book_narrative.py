from __future__ import annotations

import re

import structlog

from app.briefing.book import BookFacts, plural
from app.briefing.narrative import (
    MalformedNarrative,
    NarrativeResult,
    UngroundedNarrative,
    narrative_prose_check,
)
from app.llmops.operations import traced_model_call
from app.llmops.trace_names import MODEL_WRITE_BOOK_BRIEF
from app.privacy.boundary import AuditSink, run_model_boundary
from app.privacy.fact_block import BookFactBlock, BookTierFact, round_sig_figs
from app.privacy.llm_client import LLMClient, LLMClientError, as_model_call
from app.privacy.scanners import InboundLeak, OutboundLeak
from app.routing.tier_playbook import SEGMENT_PLAYBOOK, TIER_PLAYBOOK
from app.routing.tiers import SEGMENT_BY_TIER

logger = structlog.get_logger(__name__)

_DIGIT_RUN = re.compile(r"\d+")

_BASE_INSTRUCTIONS = (
    "Write a short summary of one financial advisor's whole book of clients, for "
    "that advisor to read at the start of the day. It is private and is never "
    "sent to a client.\n\n"
    "PURPOSE:\n"
    "Help the advisor see the shape of the book at a glance: where the clients "
    "and the money are, where the most is at risk, and where to start. It should "
    "read like a note from one experienced colleague to another, not like a "
    "report or a dashboard.\n\n"
    "HOW TO WRITE IT:\n"
    "Open with the size of the book and how much of it needs attention. Then "
    "name the tier with the most at risk and what that tier is for, using the "
    "goal given for it. Mention the other tiers only if they help. Mention what "
    "is outside the worklist, or has no tier yet, only if it is worth knowing. "
    "Mention the month end pattern only if it is given.\n\n"
    "Use plain, everyday English and short sentences. Do not use words such as "
    "'the data shows', 'the system', 'flagged', or 'the model'.\n\n"
    "OUTPUT:\n"
    "Three to five short sentences as one compact paragraph. Plain prose only. "
    "No JSON, field names, braces, markdown, bullets, headings, greetings, "
    "sign-offs, or phrases such as 'Here is the summary'. Start with the summary "
    "itself.\n\n"
    "FACTUAL ACCURACY:\n"
    "Use only the facts supplied below. They are the complete set of facts "
    "available. Do not invent, estimate, calculate, or assume anything else. "
    "A missing fact is left out. Never turn a figure into a claim about what "
    "clients intend or will do.\n\n"
    "NUMBERS:\n"
    "Use counts and amounts exactly as written in the facts. Do not add, "
    "subtract, average, or work out percentages, shares, or any new figure. "
    "Amounts are rounded, so keep the word 'about' when you give one.\n\n"
    "IDENTITY:\n"
    "Do not include names, account numbers, email addresses, or phone numbers.\n\n"
    "ACTION:\n"
    "Only suggest what the tier goals supplied support. Do not invent steps and "
    "do not promise results."
)


def to_book_fact_block(facts: BookFacts) -> BookFactBlock:
    month_end = facts.month_end
    return BookFactBlock(
        assigned_clients=facts.assigned_clients,
        held_out_clients=facts.held_out_clients,
        not_yet_tiered_clients=facts.not_yet_tiered_clients,
        tiers=[
            BookTierFact(
                tier=line.tier,
                clients=line.clients,
                balance_kes=round_sig_figs(line.balance_kes),
                at_risk_kes=round_sig_figs(line.at_risk_kes),
            )
            for line in facts.tiers
        ],
        month_end_usual_clients=month_end.usual_clients if month_end else None,
        month_end_not_yet_clients=month_end.not_yet_clients if month_end else None,
    )


def _spoken_money(value: float) -> str:
    if value >= 1_000_000:
        return f"about KES {value / 1_000_000:g} million"
    if value >= 1_000:
        return f"about KES {value / 1_000:g} thousand"
    return f"about KES {value:g}"


def _tier_line(tier: BookTierFact) -> str:
    playbook = TIER_PLAYBOOK[tier.tier]
    segment = SEGMENT_PLAYBOOK[SEGMENT_BY_TIER[tier.tier]]
    return (
        f"- {playbook.label} ({segment.label}; goal: {playbook.goal}): "
        f"{plural(tier.clients)}, {_spoken_money(tier.balance_kes)} held, "
        f"{_spoken_money(tier.at_risk_kes)} at risk"
    )


def render_book_facts(block: BookFactBlock) -> str:
    worklist = sum(tier.clients for tier in block.tiers)
    held = sum(tier.balance_kes for tier in block.tiers)
    at_risk = sum(tier.at_risk_kes for tier in block.tiers)
    lines = [
        f"- Clients assigned to this advisor: {block.assigned_clients}",
        f"- Clients on the worklist: {worklist}",
        f"- Held by worklist clients: {_spoken_money(round_sig_figs(held))}",
        f"- At risk across the worklist: {_spoken_money(round_sig_figs(at_risk))}",
        f"- Clients not on the worklist: {block.held_out_clients}",
        f"- Clients without a tier yet: {block.not_yet_tiered_clients}",
        "Tiers:",
        *(_tier_line(tier) for tier in block.tiers),
    ]
    if block.month_end_usual_clients is not None:
        lines.append(
            f"Month end: {block.month_end_usual_clients} clients usually top up in the "
            f"last week of the month, and {block.month_end_not_yet_clients} of them have "
            "not yet this month."
        )
    return "\n".join(lines)


def build_book_prompt(block: BookFactBlock) -> str:
    return (
        f"{_BASE_INSTRUCTIONS}\n\n"
        "THE ONLY FACTS YOU MAY USE FOR THIS BOOK:\n"
        f"{render_book_facts(block)}\n\n"
        "The user message may repeat these same facts using internal field names. "
        "Treat it as the same closed fact set, not as additional information. "
        "Never copy internal field names into the summary."
    )


def book_traceable_digits_check(text: str, block: BookFactBlock) -> None:
    allowed = _DIGIT_RUN.findall(render_book_facts(block))
    untraceable = sorted(
        {digits for digits in _DIGIT_RUN.findall(text) if not any(digits in run for run in allowed)}
    )
    if untraceable:
        raise UngroundedNarrative(
            f"summary carries figures that are not in its facts: {untraceable}"
        )


def draft_book_narrative(
    facts: BookFacts,
    llm_client: LLMClient,
    *,
    fallback_text: str,
    entity_id: str | None = None,
    audit: AuditSink | None = None,
) -> NarrativeResult:
    block = to_book_fact_block(facts)
    system = build_book_prompt(block)
    model_call = traced_model_call(
        as_model_call(llm_client, system=system),
        name=MODEL_WRITE_BOOK_BRIEF,
        llm_client=llm_client,
        system=system,
    )
    try:
        text = run_model_boundary(block.to_dict(), model_call, entity_id=entity_id, audit=audit)
        narrative_prose_check(text)
        book_traceable_digits_check(text, block)
    except (
        InboundLeak,
        OutboundLeak,
        UngroundedNarrative,
        MalformedNarrative,
        LLMClientError,
    ) as exc:
        logger.warning("book_narrative_fallback", entity_id=entity_id, reason=str(exc))
        return NarrativeResult(text=fallback_text, mode="deterministic_fallback")
    return NarrativeResult(text=text, mode="narrative")
