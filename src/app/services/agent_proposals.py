"""Listing, reading, and deciding on the proposals the agent writes overnight."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.proposal_state import transition_proposal
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor

_DECISION_TO_STATUS = {"approve": "approved", "reject": "rejected"}


class ProposalNotFound(Exception):
    """No agent_proposal exists with the given id."""


def _proposal_filters(
    *,
    status: str | None,
    proposal_date: date | None,
    action_code: str | None,
    exclude_action_code: str | None,
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
