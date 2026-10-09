from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action
from app.agents.guide_mix import guides_for_action
from app.audit.log import record_audit
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient, AgentProposalVariant
from app.rules.catalog import angle_is_held, load_angle

ANGLE_TEST = "angle"
GUIDE_TEST = "guide"
SPLIT_TESTS = (ANGLE_TEST, GUIDE_TEST)

SIDE_A = "A"
SIDE_B = "B"
SIDES = (SIDE_A, SIDE_B)

SPLITTABLE_STATUSES = ("proposed", "approved")


class SplitRefused(Exception):
    pass


@dataclass(frozen=True)
class Side:
    variant: str
    angle: str
    content_mix: str | None


@dataclass(frozen=True)
class Split:
    sides: dict[str, Side]
    variant_of: dict[int, str]

    def angle_for(self, client_id: int, default: str) -> str:
        variant = self.variant_of.get(client_id)
        return self.sides[variant].angle if variant in self.sides else default


def assign_sides(proposal_id: int, client_ids: Iterable[int]) -> dict[int, str]:
    ordered = sorted(set(client_ids), key=lambda client_id: _order_key(proposal_id, client_id))
    return {client_id: SIDES[place % len(SIDES)] for place, client_id in enumerate(ordered)}


def _order_key(proposal_id: int, client_id: int) -> str:
    return hashlib.sha256(f"{proposal_id}:{client_id}".encode()).hexdigest()


def split_proposal(
    session: Session,
    proposal_id: int,
    *,
    test: str,
    split_by: str,
    other_angle: str | None = None,
    as_of: date | None = None,
) -> Split:
    day = as_of or date.today()
    proposal = _splittable_proposal(session, proposal_id)
    rows = _included_rows(session, proposal_id)
    client_ids = {row.client_id for row in rows}
    if len(client_ids) < len(SIDES):
        raise SplitRefused(
            f"proposal {proposal_id} has {len(client_ids)} included clients, "
            "and each version needs at least one"
        )

    sides = _sides_for(session, proposal, test, other_angle, day)
    variant_of = assign_sides(proposal_id, client_ids)

    session.add_all(
        AgentProposalVariant(
            proposal_id=proposal_id,
            variant=side.variant,
            angle=side.angle,
            content_mix=side.content_mix,
        )
        for side in sides.values()
    )
    for row in rows:
        row.variant = variant_of[row.client_id]

    record_audit(
        session,
        entity_type="agent_proposal",
        action="split",
        entity_id=str(proposal_id),
        actor_id=split_by,
        detail={
            "test": test,
            "versions": {
                side.variant: {"angle": side.angle, "content_mix": side.content_mix}
                for side in sides.values()
            },
            "clients": {
                variant: sum(1 for assigned in variant_of.values() if assigned == variant)
                for variant in SIDES
            },
        },
    )
    session.flush()
    return Split(sides=sides, variant_of=variant_of)


def load_split(session: Session, proposal_id: int) -> Split | None:
    sides = {
        row.variant: Side(row.variant, row.angle, row.content_mix)
        for row in session.scalars(
            select(AgentProposalVariant).where(AgentProposalVariant.proposal_id == proposal_id)
        )
    }
    if not sides:
        return None
    assigned = session.execute(
        select(AgentProposalClient.client_id, AgentProposalClient.variant).where(
            AgentProposalClient.proposal_id == proposal_id,
            AgentProposalClient.included.is_(True),
            AgentProposalClient.variant.is_not(None),
        )
    ).all()
    return Split(sides=sides, variant_of=dict(assigned))


def load_split_for_campaign(session: Session, campaign_id: int) -> Split | None:
    proposal_id = session.scalar(
        select(AgentProposal.proposal_id).where(AgentProposal.campaign_id == campaign_id)
    )
    return None if proposal_id is None else load_split(session, proposal_id)


def _splittable_proposal(session: Session, proposal_id: int) -> AgentProposal:
    proposal = session.get(AgentProposal, proposal_id)
    if proposal is None:
        raise SplitRefused(f"there is no proposal {proposal_id}")
    if proposal.status not in SPLITTABLE_STATUSES:
        raise SplitRefused(
            f"proposal {proposal_id} is {proposal.status}, and only a proposal "
            "that has not started may be split"
        )
    if proposal.angle is None:
        raise SplitRefused(
            f"proposal {proposal_id} answers '{proposal.action_code}', which sends "
            "no message, so there is nothing to compare"
        )
    if session.get(AgentProposalVariant, (proposal_id, SIDE_A)) is not None:
        raise SplitRefused(f"proposal {proposal_id} is already split")
    return proposal


def _included_rows(session: Session, proposal_id: int) -> list[AgentProposalClient]:
    return list(
        session.scalars(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal_id,
                AgentProposalClient.included.is_(True),
            )
        )
    )


def _sides_for(
    session: Session, proposal: AgentProposal, test: str, other_angle: str | None, day: date
) -> dict[str, Side]:
    if test == ANGLE_TEST:
        return _two_angles(session, proposal, other_angle, day)
    if test == GUIDE_TEST:
        if other_angle is not None:
            raise SplitRefused("a guide test uses one angle, so it takes no second angle")
        return _with_and_without_guide(session, proposal, day)
    raise SplitRefused(f"there is no test called '{test}', use one of {', '.join(SPLIT_TESTS)}")


def _two_angles(
    session: Session, proposal: AgentProposal, other_angle: str | None, day: date
) -> dict[str, Side]:
    if not other_angle:
        raise SplitRefused("an angle test needs the second angle")
    if other_angle == proposal.angle:
        raise SplitRefused(f"the second angle is the same as the first, '{other_angle}'")
    if load_angle(session, other_angle, day) is None:
        raise SplitRefused(f"there is no angle '{other_angle}' in the catalogue")
    if angle_is_held(session, other_angle, day):
        raise SplitRefused(f"the angle '{other_angle}' is on hold")
    return {
        SIDE_A: Side(SIDE_A, proposal.angle, proposal.content_mix),
        SIDE_B: Side(SIDE_B, other_angle, proposal.content_mix),
    }


def _with_and_without_guide(
    session: Session, proposal: AgentProposal, day: date
) -> dict[str, Side]:
    if proposal.content_mix is None:
        raise SplitRefused("this proposal has no guide mix, so both versions would be the same")
    action = load_action(session, proposal.action_code, day)
    if action is None or not guides_for_action(session, action):
        raise SplitRefused(
            f"there is no approved client guide for '{proposal.action_code}', "
            "so both versions would read the same"
        )
    return {
        SIDE_A: Side(SIDE_A, proposal.angle, proposal.content_mix),
        SIDE_B: Side(SIDE_B, proposal.angle, None),
    }
