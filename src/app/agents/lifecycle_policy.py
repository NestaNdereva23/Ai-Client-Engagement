from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.query_fields import FilterRefused, compile_conditions
from app.audit.log import record_audit
from app.db.models.client_lifecycle import LIFECYCLE_STATES, POLICY_MODES, LifecyclePolicy

CONDITIONS_KEY = "conditions"


class PolicyRefused(ValueError):
    pass


def find_policy(session: Session, from_state: str, to_state: str) -> LifecyclePolicy | None:
    return session.scalar(
        select(LifecyclePolicy).where(
            LifecyclePolicy.from_state == from_state,
            LifecyclePolicy.to_state == to_state,
            LifecyclePolicy.is_active.is_(True),
        )
    )


def policy_conditions(policy: LifecyclePolicy) -> list[dict[str, Any]]:
    return list(policy.evidence[CONDITIONS_KEY])


def _snapshot(row: LifecyclePolicy | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "from_state": row.from_state,
        "to_state": row.to_state,
        "evidence": row.evidence,
        "mode": row.mode,
        "is_active": row.is_active,
    }


def _checked_evidence(evidence: Sequence[Any] | None) -> list[dict[str, Any]]:
    if not evidence:
        raise PolicyRefused(
            "a policy needs the evidence it rests on: a non empty list of conditions"
        )
    try:
        _, recorded = compile_conditions(evidence)
    except FilterRefused as exc:
        raise PolicyRefused(str(exc)) from None
    return recorded


def set_policy(
    session: Session,
    *,
    policy_code: str,
    from_state: str,
    to_state: str,
    evidence: Sequence[Any] | None,
    mode: str,
    description: str,
    changed_by: str,
    changed_reason: str,
    is_active: bool = True,
) -> LifecyclePolicy:
    if from_state not in LIFECYCLE_STATES or to_state not in LIFECYCLE_STATES:
        raise PolicyRefused(f"states must be among: {', '.join(LIFECYCLE_STATES)}")
    if from_state == to_state:
        raise PolicyRefused("a policy must change the state")
    if mode not in POLICY_MODES:
        raise PolicyRefused(f"mode must be one of: {', '.join(POLICY_MODES)}")
    for name, value in (
        ("policy_code", policy_code),
        ("description", description),
        ("changed_by", changed_by),
        ("changed_reason", changed_reason),
    ):
        if not str(value).strip():
            raise PolicyRefused(f"{name} cannot be empty")
    recorded = _checked_evidence(evidence)

    row = session.scalar(select(LifecyclePolicy).where(LifecyclePolicy.policy_code == policy_code))
    rival = session.scalar(
        select(LifecyclePolicy).where(
            LifecyclePolicy.from_state == from_state,
            LifecyclePolicy.to_state == to_state,
            LifecyclePolicy.policy_code != policy_code,
        )
    )
    if rival is not None:
        raise PolicyRefused(
            f"'{rival.policy_code}' already governs the change from {from_state} to {to_state}"
        )

    before = _snapshot(row)
    if row is None:
        row = LifecyclePolicy(policy_code=policy_code)
        session.add(row)
    row.from_state = from_state
    row.to_state = to_state
    row.evidence = {CONDITIONS_KEY: recorded}
    row.mode = mode
    row.is_active = is_active
    row.description = description.strip()
    row.changed_by = changed_by
    row.changed_reason = changed_reason.strip()
    session.flush()

    record_audit(
        session,
        entity_type="lifecycle_policy",
        entity_id=policy_code,
        action="create" if before is None else "update",
        actor_id=changed_by,
        detail={"before": before, "after": _snapshot(row), "reason": changed_reason.strip()},
    )
    return row
