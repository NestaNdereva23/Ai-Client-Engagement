"""The proposal state machine: the only place agent_proposal.status is written.

Everything that wants to move a proposal forward calls transition_proposal
rather than assigning .status directly, so every move is checked against the
same allowed-transition table and leaves an audit trail behind it. A terminal
state has no outgoing transitions at all, which is what actually stops
further changes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.agent_proposal import AgentProposal

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"rejected", "expired", "approved"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "approved": frozenset({"running"}),
    "running": frozenset({"blocked", "sent", "stopped"}),
    "blocked": frozenset(),
    "sent": frozenset({"measured"}),
    "stopped": frozenset(),
    "measured": frozenset(),
}

TERMINAL_STATUSES = frozenset(
    status for status, allowed in ALLOWED_TRANSITIONS.items() if not allowed
)


class InvalidTransition(Exception):
    """Raised when a status change is not one of the allowed moves from where it is."""


def transition_proposal(
    session: Session,
    proposal: AgentProposal,
    *,
    to_status: str,
    reason: str,
    decided_by: str | None = None,
) -> AgentProposal:
    """Move a proposal to to_status, or raise if that move is not allowed.

    decided_by is stamped on the proposal itself, with the time, for the move
    where a person or the act_alone permission makes the actual call. Every
    move, decided or not, is also written to the audit trail.
    """
    from_status = proposal.status
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        raise InvalidTransition(f"{from_status} to {to_status} is not an allowed move")

    proposal.status = to_status
    if decided_by is not None:
        proposal.decided_by = decided_by
        proposal.decided_at = datetime.now(UTC)
    session.flush()
    record_audit(
        session,
        entity_type="agent_proposal",
        action="transition",
        entity_id=str(proposal.proposal_id),
        actor_id=decided_by,
        detail={"from": from_status, "to": to_status, "reason": reason},
    )
    return proposal
