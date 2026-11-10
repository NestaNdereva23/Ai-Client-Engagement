from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_proposal import AgentProposal, AgentProposalVariant
from app.db.models.verified_rate import VerifiedRateFact
from app.rag.guides import find_guides
from app.rag.retrieve import Retrieved
from app.rules.verified_rate import get_verified_rate

RATE_DOC_TYPE = "verified_rate"

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


def guides_for_action(
    session: Session,
    action: AgentActionCatalog,
    *,
    doc_ids: tuple[int, ...] | None = None,
    skip_version_ids: tuple[int, ...] = (),
) -> list[Retrieved]:
    return find_guides(
        session,
        action.action_code,
        f"{action.title}. {action.who}",
        doc_ids=doc_ids,
        skip_version_ids=skip_version_ids,
    )


def guide_brief_for_campaign(
    session: Session, campaign_id: int, *, at: date | None = None, variant: str | None = None
) -> GuideBrief:
    # Imported here because permissions imports this module through action_rules.
    from app.agents.permissions import load_group_settings

    proposal = session.scalar(select(AgentProposal).where(AgentProposal.campaign_id == campaign_id))
    if proposal is None:
        return GuideBrief()
    day = at or date.today()
    group = load_group_settings(session, proposal.group_name, proposal.fa_id, day)
    rates = tuple(rate_chunks(session)) if group.use_rates else ()
    instruction = mix_instruction(_content_mix(session, proposal, variant))
    if instruction is None:
        return GuideBrief(guides=rates)
    action = load_action(session, proposal.action_code, day)
    if action is None:
        return GuideBrief(mix_instruction=instruction, guides=rates)
    skip = _last_guides(session, proposal) if group.no_repeat else ()
    guides = guides_for_action(session, action, doc_ids=group.guide_doc_ids, skip_version_ids=skip)
    return GuideBrief(mix_instruction=instruction, guides=(*guides, *rates))


def _last_guides(session: Session, proposal: AgentProposal) -> tuple[int, ...]:
    earlier = session.scalar(
        select(AgentProposal.guide_version_ids)
        .where(
            AgentProposal.group_name == proposal.group_name,
            AgentProposal.fa_id.is_(None)
            if proposal.fa_id is None
            else AgentProposal.fa_id == proposal.fa_id,
            AgentProposal.proposal_id < proposal.proposal_id,
            AgentProposal.guide_version_ids.is_not(None),
        )
        .order_by(AgentProposal.proposal_id.desc())
        .limit(1)
    )
    return tuple(earlier or ())


def rate_chunks(session: Session) -> list[Retrieved]:
    """Each product's verified rate, only while it is still fresh."""
    chunks = []
    for product in sorted(session.scalars(select(VerifiedRateFact.product).distinct())):
        row = get_verified_rate(session, product)
        if row is None:
            continue
        chunks.append(
            Retrieved(
                chunk_id=-row.fact_id,
                text=f"The latest verified {product} rate is {float(row.rate_value):g}% a year.",
                metadata={"source": "verified_rate", "product": product},
                score=1.0,
                version_id=0,
                doc_type=RATE_DOC_TYPE,
            )
        )
    return chunks


def _content_mix(session: Session, proposal: AgentProposal, variant: str | None) -> str | None:
    if variant is None:
        return proposal.content_mix
    side = session.get(AgentProposalVariant, (proposal.proposal_id, variant))
    return side.content_mix if side is not None else proposal.content_mix
