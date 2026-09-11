"""Turning one accepted finding into one proposal.

The action agent's graph and the agent's own write tool both end here, so a
proposal is written one way whichever of them asked for it: the same checks
before it, the same client rows under it, the same audit row, and the same
move of the finding to acted on.

Two rules shape the checks. A response that reaches the client runs every
contact check: the do not contact list, an open complaint, how recently the
client was contacted, and whether the angle is held. A response that stays
inside the business, a task for an adviser, an escalation, a change of
handling, or a note to watch, contacts nobody, so those checks do not apply
to it. Either way every client left out is written down with the reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.agents.agent_loop import ACTS_ALONE, GroupDecision
from app.agents.events import NO_EVENTS, EventLog
from app.agents.insight_state import transition_insight
from app.agents.permissions import effective_permission
from app.agents.propose import (
    DO_NOTHING_ACTION,
    group_skip_reasons,
    member_key,
    skip_reason_counts,
)
from app.agents.watchlist import GroupMember
from app.audit.log import record_audit
from app.db.models.agent import CONTACTING_RESPONSE_KINDS, AgentActionCatalog
from app.db.models.agent_event import APPROVAL_NEEDED, PROPOSAL_CREATED
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient

ACCEPTED = "accepted"
ACTED_ON = "acted_on"


@dataclass(frozen=True)
class InsightBrief:
    """One accepted finding, as the action agent is handed it.

    Counts, bands and the finding's own words only. No client id and no
    client name reaches this shape, the same rule everything the agent reads
    already follows.
    """

    insight_id: int
    kind: str
    title: str
    group_name: str
    client_count: int
    money_total_kes: float | None
    confidence: str
    confidence_reason: str
    suggestion: str
    why_now: str
    avoid_saying: str | None


def brief_for(insight: AgentInsight) -> InsightBrief:
    """The finding, cut down to what the model may read."""
    return InsightBrief(
        insight_id=insight.insight_id,
        kind=insight.kind,
        title=insight.title,
        group_name=insight.group_name,
        client_count=insight.client_count,
        money_total_kes=insight.money_total_kes,
        confidence=insight.confidence,
        confidence_reason=insight.confidence_reason,
        suggestion=insight.suggestion,
        why_now=insight.why_now,
        avoid_saying=insight.avoid_saying,
    )


def contact_checks_apply(action: AgentActionCatalog) -> bool:
    """Whether this response reaches the client, so the contact checks run."""
    return action.response_kind in CONTACTING_RESPONSE_KINDS


def gate_members(
    session: Session,
    *,
    action: AgentActionCatalog,
    members: Sequence[GroupMember],
    as_of: date,
    cooldown_days: int | None,
) -> dict[tuple[int, int], str | None]:
    """Why each client fund was left out of this response, or None if it stays."""
    if contact_checks_apply(action):
        return group_skip_reasons(session, members, action, as_of, cooldown_days)
    return {member_key(member): None for member in members}


def insight_evidence(brief: InsightBrief) -> str:
    """The plain language evidence behind the proposal, from the finding."""
    return (
        f"{brief.title}, covering {brief.client_count} clients. Why it matters now: {brief.why_now}"
    )


def save_insight_proposal(
    session: Session,
    *,
    insight: AgentInsight,
    decision: GroupDecision,
    members: Sequence[GroupMember],
    run_id: int | None = None,
    events: EventLog = NO_EVENTS,
) -> AgentProposal:
    """Write the proposal for one finding, its client rows, and its audit row.

    The caller owns the transaction. Every client the decision considered
    gets a row, in or out, and the finding moves to acted on so the same
    finding is not worked twice.
    """
    brief = brief_for(insight)
    proposal = AgentProposal(
        run_id=run_id,
        insight_id=insight.insight_id,
        action_code=decision.action.action_code,
        catalog_version=decision.action.version,
        group_name=brief.group_name,
        group_definition=(
            None if insight.group_definition is None else dict(insight.group_definition)
        ),
        client_count=len({member.client_id for member in members}),
        money_total_kes=sum(member.balance for member in decision.included),
        evidence=insight_evidence(brief),
        reason=decision.reason,
        angle=decision.action.message_angle,
        content_mix=decision.action.content_mix,
        permission_applied=effective_permission(session, decision.action.action_code),
        skip_reason_counts=skip_reason_counts(decision.skip_reasons),
        status="proposed",
    )
    session.add(proposal)
    session.flush()

    session.add_all(
        AgentProposalClient(
            proposal_id=proposal.proposal_id,
            client_id=member.client_id,
            unit_fund_id=member.unit_fund_id,
            included=decision.skip_reasons[member_key(member)] is None,
            skip_reason=decision.skip_reasons[member_key(member)],
        )
        for member in members
        if member_key(member) in decision.skip_reasons
    )
    session.flush()

    if insight.state == ACCEPTED:
        transition_insight(
            session,
            insight,
            to_state=ACTED_ON,
            reason=f"proposal {proposal.proposal_id} was written for this finding",
        )

    record_audit(
        session,
        entity_type="agent_proposal",
        action="create",
        entity_id=str(proposal.proposal_id),
        run_id=None if run_id is None else str(run_id),
        detail={
            "insight_id": insight.insight_id,
            "group_name": brief.group_name,
            "action_code": decision.action.action_code,
            "included_count": len(decision.included),
        },
    )
    events.record(
        PROPOSAL_CREATED,
        proposal_id=proposal.proposal_id,
        insight_id=insight.insight_id,
        group_name=brief.group_name,
        action_code=decision.action.action_code,
        included_count=len(decision.included),
        permission_applied=proposal.permission_applied,
    )
    needs_a_person = (
        decision.action.action_code != DO_NOTHING_ACTION
        and proposal.permission_applied != ACTS_ALONE
    )
    if needs_a_person:
        events.record(
            APPROVAL_NEEDED,
            proposal_id=proposal.proposal_id,
            group_name=brief.group_name,
            action_code=decision.action.action_code,
            included_count=len(decision.included),
        )
    return proposal
