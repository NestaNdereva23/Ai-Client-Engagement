from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.agents.lifecycle_workflow import enter_workflow
from app.audit.log import record_audit
from app.db.models.client_lifecycle import ClientLifecycle, LifecyclePolicy


def change_state(
    session: Session,
    *,
    client_id: int,
    unit_fund_id: int,
    policy: LifecyclePolicy,
    insight_id: int,
    changed_by: str,
) -> ClientLifecycle:
    workflow = enter_workflow(
        session,
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        from_state=policy.from_state,
        to_state=policy.to_state,
        policy_code=policy.policy_code,
    )
    row = session.get(ClientLifecycle, (client_id, unit_fund_id))
    if row is None:
        row = ClientLifecycle(client_id=client_id, unit_fund_id=unit_fund_id)
        session.add(row)
    row.state = policy.to_state
    row.workflow = workflow
    row.policy_code = policy.policy_code
    row.insight_id = insight_id
    row.changed_by = changed_by
    row.changed_at = datetime.now(UTC)
    session.flush()

    record_audit(
        session,
        entity_type="client_lifecycle",
        entity_id=f"{client_id}/{unit_fund_id}",
        action="change",
        actor_id=changed_by,
        detail={
            "from": policy.from_state,
            "to": policy.to_state,
            "policy_code": policy.policy_code,
            "mode": policy.mode,
            "insight_id": insight_id,
            "workflow": workflow,
        },
    )
    return row
