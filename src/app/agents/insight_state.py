"""The insight state machine: the only place agent_insight.state is written.

Everything that wants to move a finding forward calls transition_insight
rather than assigning .state directly, so every move is checked against the
same allowed-transition table and leaves an audit trail behind it. A
terminal state has no outgoing transitions at all, which is what actually
stops further changes. Dismissing needs a reason, and the reason is kept.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.agent_insight import AgentInsight

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"accepted", "dismissed", "expired"}),
    "accepted": frozenset({"acted_on", "dismissed", "expired"}),
    "acted_on": frozenset(),
    "dismissed": frozenset(),
    "expired": frozenset(),
}

TERMINAL_STATES = frozenset(state for state, allowed in ALLOWED_TRANSITIONS.items() if not allowed)


class InvalidTransition(Exception):
    """Raised when a state change is not one of the allowed moves from where it is."""


def transition_insight(
    session: Session,
    insight: AgentInsight,
    *,
    to_state: str,
    reason: str,
    decided_by: str | None = None,
) -> AgentInsight:
    """Move a finding to to_state, or raise if that move is not allowed.

    decided_by is stamped on the finding itself, with the time, for the move
    where a person makes the actual call. A move to dismissed keeps the
    reason on the row as well as in the audit trail.
    """
    from_state = insight.state
    if to_state not in ALLOWED_TRANSITIONS.get(from_state, frozenset()):
        raise InvalidTransition(f"{from_state} to {to_state} is not an allowed move")
    if to_state == "dismissed" and not reason.strip():
        raise InvalidTransition("dismissing a finding needs a reason")

    insight.state = to_state
    if to_state == "dismissed":
        insight.dismissed_reason = reason
    if decided_by is not None:
        insight.decided_by = decided_by
        insight.decided_at = datetime.now(UTC)
    session.flush()
    record_audit(
        session,
        entity_type="agent_insight",
        action="transition",
        entity_id=str(insight.insight_id),
        actor_id=decided_by,
        detail={"from": from_state, "to": to_state, "reason": reason},
    )
    return insight
