"""System prompt and fact rendering
The model receives a closed set of client facts rendered in plain English.
It must stay strictly within those facts and turn them into a useful,
natural sounding pointer for the advisor.
"""

from __future__ import annotations

from typing import Any

from app.privacy.fact_block import RiskFactBlock

# Human-readable labels used when presenting band level facts to the model.
# These deliberately describe what the information means to an advisor rather
# than exposing internal field names. The model should write from the meaning
# of the fact, not repeat the label itself.
BAND_LABELS: tuple[tuple[str, str], ...] = (
    ("fund_name", "Fund they hold"),
    ("risk_band", "Overall attention level"),
    ("route", "Suggested next step"),
    ("balance_tier", "Size of their current holding"),
    ("value_tier", "Usual deposit size"),
    ("recency_band", "Time since their last deposit"),
    ("deposit_trend_band", "Recent direction of their deposit sizes"),
)


# Plain-language descriptions for the internal route values.
#
# These should describe the intended next step without adding conclusions
# that are not explicitly supported by the route itself.
ROUTE_PHRASES = {
    "small_balance_review": ("review the holding before deciding whether contact is needed"),
    "fa_call_priority": "call this client soon",
    "fa_watchlist": "keep this client on the watchlist",
    "auto_checkin": "let the automated check in handle the follow-up",
    "monitor_only": "no action is needed yet; continue monitoring",
}


# Plain-language descriptions for risk signals.
# These intentionally avoid numbers and internal signal names. They describe
# the underlying observation in a way that can naturally appear in a briefing.
SIGNAL_PHRASES: tuple[tuple[str, str], ...] = (
    (
        "sig_broken_pattern",
        "they have gone far longer than their own usual gap without depositing",
    ),
    (
        "sig_dormant",
        "they have not deposited for about a year or more",
    ),
    (
        "sig_heavy_withdrawal",
        "they took out a large share of what they held",
    ),
    (
        "sig_shrinking",
        "each deposit has been smaller than the one before",
    ),
    (
        "sig_going_dormant",
        "fees will use up what is left before long",
    ),
    (
        "sig_never_repeated",
        "they deposited once and never came back",
    ),
)


# Caveats that may affect how the advisor should interpret the situation.
#
# These are written as useful context rather than technical data-quality
# terminology.
CAVEAT_PHRASES: tuple[tuple[str, str], ...] = (
    (
        "deposit_count_capped",
        "only their most recent few deposits are visible, so their real deposit pattern may differ",
    ),
    (
        "withdrawal_history_hidden",
        "their withdrawal history is not visible to us",
    ),
    (
        "holds_both_funds",
        "they also hold a position in the other fund",
    ),
    (
        "has_open_complaint",
        "they have an open complaint",
    ),
)


_BASE_INSTRUCTIONS = (
    "Write a short internal briefing for a financial advisor who is about "
    "to review or contact one client. The briefing is private and is not "
    "sent to the client.\n\n"
    "PURPOSE:\n"
    "Help the advisor quickly understand what is happening, why this client "
    "needs attention, and what the advisor should consider doing next. The "
    "briefing should feel like a useful pointer from one colleague to another, "
    "not like a report, dashboard summary, or list of database findings.\n\n"
    "HOW TO WRITE IT:\n"
    "Start with the client's current position when the available facts support "
    "it. Where useful, connect what they hold with how long they have been "
    "quiet and the recent direction of their deposits. Then explain the main "
    "reason they need attention. Finish with any important context or "
    "limitation and the suggested next step, if one is provided.\n\n"
    "Do not mechanically mention every fact. Choose the facts that help the "
    "advisor understand the situation. Related facts should be combined into "
    "a natural sentence rather than stated one after another.\n\n"
    "Think of the note as a quick handover from one experienced colleague "
    "to another. The advisor should be able to read it in a few seconds and "
    "understand what deserves attention.\n\n"
    "Use plain, everyday English. Keep sentences short and natural. Vary "
    "sentence structure where appropriate. Use simple transitions such as "
    "'The main concern is', 'It is worth noting that', or 'The picture is "
    "incomplete because' only when they genuinely improve the flow. Do not "
    "force these phrases into every note.\n\n"
    "Avoid language that sounds like a system, report, dashboard, or model. "
    "Do not say things such as 'the risk band indicates', 'the deposit trend "
    "shows', 'the system detected', 'this client was flagged because', or "
    "'the data suggests' when the useful point can be stated directly. Do "
    "not expose internal terminology or explain how the scoring works.\n\n"
    "Do not exaggerate the situation. A warning sign is an observation, not "
    "proof that the client intends to leave, has a problem, or will take a "
    "particular action. Describe only what the supplied facts support.\n\n"
    "OUTPUT:\n"
    "Write three to five short sentences as one or two compact paragraphs. "
    "The note should be easy to scan in a few seconds.\n\n"
    "Return plain prose only. No JSON, key-value pairs, field names, braces, "
    "markdown, bullets, headings, greetings, sign-offs, or introductory "
    "phrases such as 'Here is the briefing'. Start immediately with the note.\n\n"
    "FACTUAL ACCURACY:\n"
    "Use only the facts supplied below. They are the complete set of facts "
    "available for this client. Do not invent, estimate, calculate, infer, "
    "or assume anything that is not explicitly provided.\n\n"
    "If a fact is missing, leave it out. Missing information does not mean "
    "that the opposite is true. For example, if there is no information "
    "about withdrawals, do not say that the client has not withdrawn money. "
    "If there is no complaint information, do not mention complaints.\n\n"
    "Never combine separate facts to create a new conclusion. Do not turn "
    "a warning sign into a claim about the client's intentions, feelings, "
    "financial situation, or future behaviour.\n\n"
    "NUMBERS:\n"
    " Use the suppplied digits, percentages, monetary amounts, or dates."
    "plain wording supplied for each band or category instead. Never turn "
    "a band into an estimated number or a more precise description.\n\n"
    "IDENTITY:\n"
    "Do not include names, account numbers, email addresses, phone numbers, "
    "or other identifying information. Refer to the person only as "
    "'this client' or 'they'.\n\n"
    "ACTION:\n"
    "Only mention a next step when one is supplied in the facts. Do not "
    "invent a recommendation.\n\n"
    "If the supplied action is to call, say so plainly. If the supplied "
    "action is to monitor, make that clear without making the situation "
    "sound urgent. If the supplied action is to use an automated check-in, "
    "do not turn it into a recommendation for a personal call.\n\n"
    "CONTEXT AND LIMITATIONS:\n"
    "If a limitation affects how the advisor should read the situation, "
    "mention it briefly. Do not turn the note into a disclaimer or a "
    "technical explanation. The purpose is to give the advisor useful "
    "context, not to explain the data or the system.\n\n"
    "NATURALNESS:\n"
    "Before answering, silently check that the note sounds natural when "
    "spoken aloud by one advisor to another. Remove repeated ideas, awkward "
    "labels, unnecessary qualifiers, database language, and phrases that "
    "sound generated. Do not mention this check in the answer.\n\n"
    "STYLE EXAMPLE:\n"
    "This client holds a position in the money market fund and has been "
    "quiet for several months. The main concern is that they have gone much "
    "longer than their usual gap between deposits, while their deposit sizes "
    "have also been getting smaller. Their withdrawal history is not visible, "
    "so there is some context we may be missing. They are on the watchlist, "
    "so this is worth keeping in mind at the next review.\n\n"
    "The example only demonstrates the writing style. Its facts are "
    "illustrative and must never be copied unless the same facts are "
    "explicitly present in the supplied facts for this client."
    "Balance tier	Balance"
    "Tiny	KSh 100 or under"
    "Micro	KSh 100 to KSh 1,000"
    "Small	KSh 1,000 to KSh 10,000"
    "Core	KSh 10,000 to KSh 100,000"
    "Premium	KSh 100,000 to KSh 1,000,000"
    "Institutional	Over KSh 1,000,000"
)


def narrative_facts(facts: RiskFactBlock) -> dict[str, Any]:
    """Return only facts the narrative is allowed to draw on.

    False boolean values are removed because a signal that did not fire is
    not useful information for the narrative. Sending false signals to the
    model can encourage it to mention or reason about events that did not
    happen.

    Present bands and true boolean values are retained exactly as carried
    by the RiskFactBlock.
    """
    return {key: value for key, value in facts.to_dict().items() if value is not False}


def _render_facts(payload: dict[str, Any]) -> str:
    """Render the closed fact set in plain language for the model.

    Internal field names are translated into descriptions that explain the
    meaning of each fact. This gives the model useful context without asking
    it to interpret internal schema terminology.
    """
    lines: list[str] = []

    # Render band-level facts in an order that supports a natural briefing:
    # current position -> context -> suggested action.
    for key, label in BAND_LABELS:
        if key not in payload:
            continue

        value = payload[key]

        if key == "route":
            value = ROUTE_PHRASES.get(str(value), value)

        lines.append(f"- {label}: {value}")

    # Only include warning signs that actually fired.
    fired = [phrase for key, phrase in SIGNAL_PHRASES if payload.get(key)]

    if fired:
        lines.append("Warning signs for this client:")
        lines.extend(f"- {phrase}" for phrase in fired)

    # Include only caveats that are actually present.
    caveats = [phrase for key, phrase in CAVEAT_PHRASES if payload.get(key)]

    if caveats:
        lines.append("Relevant context or limits:")
        lines.extend(f"- {phrase}" for phrase in caveats)

    if not lines:
        return "(no facts are available for this client)"

    return "\n".join(lines)


def build_narrative_prompt(facts: RiskFactBlock) -> str:
    """Build the complete system prompt for one advisor briefing.

    The model receives a plain-language rendering of the closed fact set.
    The same payload may also be passed through the model boundary as a
    scanned user turn. That second representation is still the same fact
    set and must not be treated as additional information.
    """
    rendered_facts = _render_facts(narrative_facts(facts))

    return (
        f"{_BASE_INSTRUCTIONS}\n\n"
        "THE ONLY FACTS YOU MAY USE FOR THIS CLIENT:\n"
        f"{rendered_facts}\n\n"
        "The user message may repeat these same facts using internal field "
        "names. Treat it as the same closed fact set, not as additional "
        "information. Never copy internal field names into the briefing."
    )
