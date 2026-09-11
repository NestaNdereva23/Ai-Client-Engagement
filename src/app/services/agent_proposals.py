"""Listing, reading, and deciding on the proposals the agent writes overnight."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.proposal_state import transition_proposal
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor

_DECISION_TO_STATUS = {"approve": "approved", "reject": "rejected"}

# A proposal in either of these statuses never reached a client, so it never
# used any of the day's allowance. Every other status, including one still
# waiting on a decision, counts as committed.
ALLOWANCE_EXCLUDED_STATUSES = ("rejected", "expired")


class ProposalNotFound(Exception):
    """No agent_proposal exists with the given id."""


def _proposal_filters(
    *,
    status: str | None,
    proposal_date: date | None,
    action_code: str | None,
    exclude_action_code: str | None,
    group_name: str | None = None,
) -> list[Any]:
    clauses: list[Any] = []
    if status is not None:
        clauses.append(AgentProposal.status == status)
    if proposal_date is not None:
        clauses.append(func.date(AgentProposal.created_at) == proposal_date)
    if action_code is not None:
        clauses.append(AgentProposal.action_code == action_code)
    if exclude_action_code is not None:
        clauses.append(AgentProposal.action_code != exclude_action_code)
    if group_name is not None:
        clauses.append(AgentProposal.group_name == group_name)
    return clauses


def _included_counts(session: Session, proposal_ids: list[int]) -> dict[int, int]:
    """How many clients are actually included on each of the given proposals."""
    if not proposal_ids:
        return {}
    rows = session.execute(
        select(AgentProposalClient.proposal_id, func.count())
        .where(
            AgentProposalClient.proposal_id.in_(proposal_ids),
            AgentProposalClient.included.is_(True),
        )
        .group_by(AgentProposalClient.proposal_id)
    ).all()
    return dict(rows)


def list_proposals(
    session: Session,
    *,
    status: str | None = None,
    proposal_date: date | None = None,
    action_code: str | None = None,
    exclude_action_code: str | None = None,
    group_name: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[tuple[AgentProposal, int | None]], str | None]:
    """One page of proposals, newest first, each paired with how many of its
    clients are actually included (None when the proposal has no client rows
    at all, which only happens if something upstream never wrote them).
    """
    limit = clamp_limit(limit)
    filters = _proposal_filters(
        status=status,
        proposal_date=proposal_date,
        action_code=action_code,
        exclude_action_code=exclude_action_code,
        group_name=group_name,
    )
    query = select(AgentProposal).where(*filters)
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(AgentProposal.proposal_id < after_id)
    query = query.order_by(AgentProposal.proposal_id.desc()).limit(limit + 1)

    rows = list(session.scalars(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].proposal_id)

    included_counts = _included_counts(session, [row.proposal_id for row in rows])
    paired = [(row, included_counts.get(row.proposal_id)) for row in rows]
    return paired, next_cursor


def count_proposals(
    session: Session,
    *,
    status: str | None = None,
    proposal_date: date | None = None,
    action_code: str | None = None,
    exclude_action_code: str | None = None,
    group_name: str | None = None,
) -> int:
    """How many proposals list_proposals' filters would return in total."""
    return session.scalar(
        select(func.count())
        .select_from(AgentProposal)
        .where(
            *_proposal_filters(
                status=status,
                proposal_date=proposal_date,
                action_code=action_code,
                exclude_action_code=exclude_action_code,
                group_name=group_name,
            )
        )
    )


def get_proposal(session: Session, proposal_id: int) -> AgentProposal:
    """One proposal, or raise ProposalNotFound."""
    proposal = session.get(AgentProposal, proposal_id)
    if proposal is None:
        raise ProposalNotFound(proposal_id)
    return proposal


def get_proposal_clients(session: Session, proposal_id: int) -> list[AgentProposalClient]:
    """Every client the proposal considered, in the order they were written."""
    return list(
        session.scalars(
            select(AgentProposalClient)
            .where(AgentProposalClient.proposal_id == proposal_id)
            .order_by(AgentProposalClient.proposal_client_id)
        ).all()
    )


@dataclass(frozen=True)
class DailyUsage:
    """How much of one action's daily allowance its proposals have used up."""

    used_clients: int
    used_money_kes: float


def daily_usage(session: Session, *, action_code: str, as_of: date) -> DailyUsage:
    """Clients and money this action's proposals have committed to today.

    Money is read straight off agent_proposal.money_total_kes, which
    propose_group already computes from the included clients only. Clients
    are counted from agent_proposal_client instead, since
    agent_proposal.client_count is the whole group, included and excluded
    members together.
    """
    matches = [
        AgentProposal.action_code == action_code,
        func.date(AgentProposal.created_at) == as_of,
        AgentProposal.status.not_in(ALLOWANCE_EXCLUDED_STATUSES),
    ]
    used_money = session.scalar(
        select(func.coalesce(func.sum(AgentProposal.money_total_kes), 0.0)).where(*matches)
    )
    used_clients = session.scalar(
        select(func.count(func.distinct(AgentProposalClient.client_id)))
        .select_from(AgentProposal)
        .join(AgentProposalClient, AgentProposalClient.proposal_id == AgentProposal.proposal_id)
        .where(*matches, AgentProposalClient.included.is_(True))
    )
    return DailyUsage(used_clients=used_clients or 0, used_money_kes=float(used_money or 0.0))


def decide_proposal(
    session: Session,
    proposal_id: int,
    *,
    decision: str,
    reason: str,
    decided_by: str,
) -> AgentProposal:
    """Approve or reject one proposal, recording who decided and why."""
    proposal = get_proposal(session, proposal_id)
    return transition_proposal(
        session,
        proposal,
        to_status=_DECISION_TO_STATUS[decision],
        reason=reason,
        decided_by=decided_by,
    )
