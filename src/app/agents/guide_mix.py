from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action
from app.db.models.agent_proposal import AgentProposal
from app.rag.guides import find_guides
from app.rag.retrieve import Retrieved

MIX_LABELS = {
    "learning_only": "Guiding only",
    "mostly_learning": "Mostly guiding",
    "balanced": "Balanced",
    "mostly_ask": "Mostly asking",
}

MIX_INSTRUCTIONS = {
    "learning_only": (
        "This message only explains. Use the client guide to explain one idea in plain "
        "words. Do not ask the client to deposit, call or decide anything, and leave out "
        "the ask in the angle above. You may say the client can reply with questions."
    ),
    "mostly_learning": (
        "This message mostly explains. Spend most of it explaining one idea from the "
        "client guide. End with one soft invitation that fits the angle above, and make "
        "it easy to ignore."
    ),
    "balanced": (
        "This message explains and asks in about equal parts. Give a short explanation "
        "from the client guide, then make the one clear ask from the angle above."
    ),
    "mostly_ask": (
        "This message mostly asks. Lead with the ask from the angle above. Add at most "
        "one sentence from the client guide to say why it helps, and explain no more "
        "than that."
    ),
}

GUIDE_RULE = (
    "Explain only what the client guide in the facts below says. If no client guide "
    "is listed there, do not explain anything about the product."
)


@dataclass(frozen=True)
class GuideBrief:
    mix_instruction: str | None = None
    guides: tuple[Retrieved, ...] = ()


def mix_instruction(content_mix: str | None) -> str | None:
    instruction = MIX_INSTRUCTIONS.get(content_mix or "")
    if instruction is None:
        return None
    return f"{instruction} {GUIDE_RULE}"


def guide_brief_for_campaign(
    session: Session, campaign_id: int, *, at: date | None = None
) -> GuideBrief:
    proposal = session.scalar(select(AgentProposal).where(AgentProposal.campaign_id == campaign_id))
    if proposal is None:
        return GuideBrief()
    instruction = mix_instruction(proposal.content_mix)
    if instruction is None:
        return GuideBrief()
    action = load_action(session, proposal.action_code, at or date.today())
    if action is None:
        return GuideBrief(mix_instruction=instruction)
    guides = find_guides(session, action.action_code, f"{action.title}. {action.who}")
    return GuideBrief(mix_instruction=instruction, guides=tuple(guides))
