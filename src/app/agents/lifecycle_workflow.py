from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.active_clients import FLAGGED_FOR_ACCOUNT_MANAGER
from app.db.models.client_lifecycle import ACTIVE_STATE, DORMANT_STATE
from app.services.active_clients import record_interaction

LIFECYCLE_ACTOR = "lifecycle_rules"

ACTIVE_WORKFLOW = "active_watch"
DORMANT_WORKFLOW = "dormant_follow_up"

WORKFLOW_FOR_STATE: dict[str, str] = {
    ACTIVE_STATE: ACTIVE_WORKFLOW,
    DORMANT_STATE: DORMANT_WORKFLOW,
}


def enter_workflow(
    session: Session,
    *,
    client_id: int,
    unit_fund_id: int,
    from_state: str,
    to_state: str,
    policy_code: str,
) -> str:
    if to_state == DORMANT_STATE:
        record_interaction(
            session,
            client_id,
            unit_fund_id,
            type=FLAGGED_FOR_ACCOUNT_MANAGER,
            note=(
                f"Moved from {from_state} to {to_state} under the rule {policy_code}. "
                "Follow the dormant plan for this client."
            ),
            reviewer_id=LIFECYCLE_ACTOR,
        )
    return WORKFLOW_FOR_STATE[to_state]
