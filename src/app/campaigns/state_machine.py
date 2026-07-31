"""The enrollment state machine: the only place enrollment.status is written.

Everything that wants to move an enrollment forward calls
transition_enrollment rather than assigning .status directly, so every
change is checked against the same allowed-transition table and leaves an
audit trail behind it. A terminal state has no outgoing transitions at all,
which is what actually stops further touches: nothing can move it anywhere
else, and the scheduler only ever selects enrolled or in_progress rows to
begin with.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.campaigns import Enrollment

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "enrolled": frozenset({"in_progress", "excluded"}),
    "in_progress": frozenset(
        {
            "in_progress",
            "completed",
            "stopped_reply",
            "stopped_optout",
            "stopped_bounce",
            "stopped_reengaged",
        }
    ),
    "excluded": frozenset(),
    "completed": frozenset(),
    "stopped_reply": frozenset(),
    "stopped_optout": frozenset(),
    "stopped_bounce": frozenset(),
    "stopped_reengaged": frozenset(),
}

TERMINAL_STATUSES = frozenset(
    status for status, allowed in ALLOWED_TRANSITIONS.items() if not allowed
)


class InvalidTransition(Exception):
    """Raised when a status change is not one of the allowed moves from where it is."""


def transition_enrollment(
    session: Session, enrollment: Enrollment, *, to_status: str, reason: str
) -> Enrollment:
    """Move an enrollment to to_status, or raise if that move is not allowed.

    A self-transition (in_progress to in_progress, one touch after another)
    is allowed and still audited like any other move.
    """
    from_status = enrollment.status
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        raise InvalidTransition(f"{from_status} to {to_status} is not an allowed move")

    enrollment.status = to_status
    session.flush()
    record_audit(
        session,
        entity_type="enrollment",
        action="transition",
        entity_id=str(enrollment.enrollment_id),
        detail={"from": from_status, "to": to_status, "reason": reason},
    )
    return enrollment
